"""
Tests für get_token.py.

main() bleibt bewusst ungetestet - der Flow ist vollständig interaktiv
(input(), Umgebungs-Prompts) und orchestriert nur die beiden hier getesteten
Funktionen plus Datei-I/O. load_client_secrets() und get_authorization_code()
enthalten die eigentliche Logik (Parsing, Validierung, HTTP-Austausch) und
sind isoliert testbar.
"""

import builtins
import json
import socket
import threading
import time

import pytest
import requests


class FakeTokenResponse:
    """Minimales Double für requests.Response bei requests.post()."""

    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


def _write_secrets_file(tmp_path, content):
    path = tmp_path / "client_secrets.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f)
    return str(path)


class _FakePwEntry:
    def __init__(self, uid, gid):
        self.pw_uid = uid
        self.pw_gid = gid


class TestChownToServiceUser:
    def test_chowns_to_existing_service_user(self, get_token_module, tmp_path, monkeypatch):
        target = tmp_path / "creds.json"
        target.write_text("{}", encoding="utf-8")
        chown_calls = []

        monkeypatch.setattr(get_token_module.pwd, "getpwnam", lambda name: _FakePwEntry(1234, 5678))
        monkeypatch.setattr(get_token_module.os, "chown", lambda path, uid, gid: chown_calls.append((path, uid, gid)))

        get_token_module._chown_to_service_user(str(target), "yt-upload")

        assert chown_calls == [(str(target), 1234, 5678)]

    def test_missing_service_user_is_not_an_error(self, get_token_module, tmp_path, monkeypatch):
        target = tmp_path / "creds.json"
        target.write_text("{}", encoding="utf-8")

        def raise_keyerror(name):
            raise KeyError(name)

        monkeypatch.setattr(get_token_module.pwd, "getpwnam", raise_keyerror)

        get_token_module._chown_to_service_user(str(target), "yt-upload")  # darf nicht raisen

    def test_permission_error_on_chown_is_not_an_error(self, get_token_module, tmp_path, monkeypatch):
        target = tmp_path / "creds.json"
        target.write_text("{}", encoding="utf-8")

        def raise_permission_error(path, uid, gid):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(get_token_module.pwd, "getpwnam", lambda name: _FakePwEntry(1234, 5678))
        monkeypatch.setattr(get_token_module.os, "chown", raise_permission_error)

        get_token_module._chown_to_service_user(str(target), "yt-upload")  # darf nicht raisen


class TestLoadClientSecrets:
    def test_installed_format_with_defaults(self, get_token_module, tmp_path, monkeypatch):
        secrets_path = _write_secrets_file(tmp_path, {
            "installed": {"client_id": "id1", "client_secret": "sec1"}
        })
        monkeypatch.setattr(get_token_module, "CLIENT_SECRETS_FILE", secrets_path)

        client_id, client_secret, auth_uri, token_uri = get_token_module.load_client_secrets()

        assert client_id == "id1"
        assert client_secret == "sec1"
        assert auth_uri == "https://accounts.google.com/o/oauth2/v2/auth"
        assert token_uri == "https://oauth2.googleapis.com/token"

    def test_web_format_with_custom_uris(self, get_token_module, tmp_path, monkeypatch):
        secrets_path = _write_secrets_file(tmp_path, {
            "web": {
                "client_id": "id2",
                "client_secret": "sec2",
                "auth_uri": "https://custom/auth",
                "token_uri": "https://custom/token",
            }
        })
        monkeypatch.setattr(get_token_module, "CLIENT_SECRETS_FILE", secrets_path)

        client_id, client_secret, auth_uri, token_uri = get_token_module.load_client_secrets()

        assert (client_id, client_secret, auth_uri, token_uri) == (
            "id2", "sec2", "https://custom/auth", "https://custom/token"
        )

    def test_flat_format_without_installed_or_web_wrapper(self, get_token_module, tmp_path, monkeypatch):
        secrets_path = _write_secrets_file(tmp_path, {"client_id": "id3", "client_secret": "sec3"})
        monkeypatch.setattr(get_token_module, "CLIENT_SECRETS_FILE", secrets_path)

        client_id, client_secret, _, _ = get_token_module.load_client_secrets()

        assert client_id == "id3"
        assert client_secret == "sec3"

    def test_missing_client_id_raises_value_error(self, get_token_module, tmp_path, monkeypatch):
        secrets_path = _write_secrets_file(tmp_path, {"installed": {"client_secret": "sec"}})
        monkeypatch.setattr(get_token_module, "CLIENT_SECRETS_FILE", secrets_path)

        with pytest.raises(ValueError, match="client_id"):
            get_token_module.load_client_secrets()

    def test_missing_client_secret_raises_value_error(self, get_token_module, tmp_path, monkeypatch):
        secrets_path = _write_secrets_file(tmp_path, {"installed": {"client_id": "id"}})
        monkeypatch.setattr(get_token_module, "CLIENT_SECRETS_FILE", secrets_path)

        with pytest.raises(ValueError, match="client_secret"):
            get_token_module.load_client_secrets()


class TestGetAuthorizationCode:
    @pytest.fixture(autouse=True)
    def fixed_state(self, get_token_module, monkeypatch):
        """Macht den zufälligen OAuth-State deterministisch testbar."""
        monkeypatch.setattr(get_token_module.secrets, "token_urlsafe", lambda n: "FIXEDSTATE")

    def test_successful_exchange_returns_token_response(self, get_token_module, monkeypatch):
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt: "http://localhost:8080/?state=FIXEDSTATE&code=AUTHCODE123"
        )
        monkeypatch.setattr(
            get_token_module.requests, "post",
            lambda *a, **k: FakeTokenResponse(200, json_data={"access_token": "AT", "refresh_token": "RT"})
        )

        result = get_token_module.get_authorization_code(
            "https://auth", "https://token", "cid", "http://localhost:8080/", "csecret"
        )

        assert result == {"access_token": "AT", "refresh_token": "RT"}

    def test_error_param_in_redirect_url_raises(self, get_token_module, monkeypatch):
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt: "http://localhost:8080/?error=access_denied&state=FIXEDSTATE"
        )

        with pytest.raises(RuntimeError, match="abgebrochen"):
            get_token_module.get_authorization_code(
                "https://auth", "https://token", "cid", "http://localhost:8080/", "csecret"
            )

    def test_state_mismatch_raises(self, get_token_module, monkeypatch):
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt: "http://localhost:8080/?state=WRONGSTATE&code=AUTHCODE123"
        )

        with pytest.raises(RuntimeError, match="State"):
            get_token_module.get_authorization_code(
                "https://auth", "https://token", "cid", "http://localhost:8080/", "csecret"
            )

    def test_missing_code_raises(self, get_token_module, monkeypatch):
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt: "http://localhost:8080/?state=FIXEDSTATE"
        )

        with pytest.raises(RuntimeError, match="Autorisierung"):
            get_token_module.get_authorization_code(
                "https://auth", "https://token", "cid", "http://localhost:8080/", "csecret"
            )

    def test_failed_token_exchange_raises(self, get_token_module, monkeypatch):
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt: "http://localhost:8080/?state=FIXEDSTATE&code=AUTHCODE123"
        )
        monkeypatch.setattr(
            get_token_module.requests, "post",
            lambda *a, **k: FakeTokenResponse(400, text="invalid_grant")
        )

        with pytest.raises(RuntimeError, match="Token-Austausch"):
            get_token_module.get_authorization_code(
                "https://auth", "https://token", "cid", "http://localhost:8080/", "csecret"
            )

    def test_local_server_flow_used_when_enabled(self, get_token_module, monkeypatch):
        """
        OAUTH_LOCAL_SERVER=true: input() darf gar nicht erst aufgerufen werden,
        stattdessen wird der Browser geöffnet und auf den lokalen Server gewartet.
        _await_redirect_via_local_server() selbst hat ihren eigenen End-to-End-Test
        (TestAwaitRedirectViaLocalServer) und wird hier bewusst gemockt.
        """
        monkeypatch.setattr(get_token_module, "USE_LOCAL_SERVER", True)
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt: (_ for _ in ()).throw(AssertionError("input() sollte bei OAUTH_LOCAL_SERVER nicht aufgerufen werden"))
        )
        opened_urls = []
        monkeypatch.setattr(get_token_module.webbrowser, "open", lambda url: opened_urls.append(url))
        monkeypatch.setattr(
            get_token_module, "_await_redirect_via_local_server",
            lambda redirect_uri: "state=FIXEDSTATE&code=AUTHCODE123"
        )
        monkeypatch.setattr(
            get_token_module.requests, "post",
            lambda *a, **k: FakeTokenResponse(200, json_data={"access_token": "AT", "refresh_token": "RT"})
        )

        result = get_token_module.get_authorization_code(
            "https://auth", "https://token", "cid", "http://localhost:8080/", "csecret"
        )

        assert result == {"access_token": "AT", "refresh_token": "RT"}
        assert opened_urls and opened_urls[0].startswith("https://auth?")

    def test_local_server_flow_still_validates_state(self, get_token_module, monkeypatch):
        monkeypatch.setattr(get_token_module, "USE_LOCAL_SERVER", True)
        monkeypatch.setattr(get_token_module.webbrowser, "open", lambda url: None)
        monkeypatch.setattr(
            get_token_module, "_await_redirect_via_local_server",
            lambda redirect_uri: "state=WRONGSTATE&code=AUTHCODE123"
        )

        with pytest.raises(RuntimeError, match="State"):
            get_token_module.get_authorization_code(
                "https://auth", "https://token", "cid", "http://localhost:8080/", "csecret"
            )


class TestAwaitRedirectViaLocalServer:
    def test_captures_query_string_from_incoming_request(self, get_token_module):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("localhost", 0))
            port = probe.getsockname()[1]
        redirect_uri = f"http://localhost:{port}/"

        result = {}

        def _run():
            result["query"] = get_token_module._await_redirect_via_local_server(redirect_uri)

        thread = threading.Thread(target=_run)
        thread.start()

        deadline = time.monotonic() + 5
        response = None
        last_error = None
        while time.monotonic() < deadline and response is None:
            try:
                response = requests.get(f"{redirect_uri}?code=ABC123&state=XYZ", timeout=1)
            except requests.exceptions.ConnectionError as e:
                last_error = e
                time.sleep(0.05)

        thread.join(timeout=5)

        assert response is not None, f"Lokaler Server nie erreichbar geworden: {last_error}"
        assert response.status_code == 200
        assert "Authentifizierung abgeschlossen" in response.text
        assert result["query"] == "code=ABC123&state=XYZ"

    def test_port_already_in_use_raises_runtime_error(self, get_token_module):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("localhost", 0))
            port = blocker.getsockname()[1]
            blocker.listen(1)

            with pytest.raises(RuntimeError, match="lokalen OAuth-Callback-Server"):
                get_token_module._await_redirect_via_local_server(f"http://localhost:{port}/")

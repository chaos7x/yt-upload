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

import pytest


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

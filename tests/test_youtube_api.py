"""
Tests für get_access_token() und add_video_to_playlist() in youtube_api.py.

Ergänzt test_chunk_upload_retry.py, das ausschließlich die Chunk-Upload-Logik
von upload_single_video() abdeckt (dort werden get_access_token() und
add_video_to_playlist() bewusst übersprungen, indem thumb_path=None und
playlist_name=None übergeben werden). Alle HTTP-Aufrufe werden über
requests.get()/requests.post() gemockt, es findet kein echter Netzwerk-Zugriff statt.
"""

import json
import os

import pytest


class FakeResponse:
    """Minimales Double für requests.Response - nur was der Code tatsächlich nutzt."""

    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


def _write_credentials(path, data, mode=0o600):
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, mode)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch, youtube_api):
    """Verhindert echte time.sleep()-Aufrufe während der Retry-Tests in add_video_to_playlist()."""
    monkeypatch.setattr(youtube_api.time, "sleep", lambda seconds: None)


class TestGetAccessToken:
    def test_success_returns_access_token_and_sends_refresh_token(self, youtube_api, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.json"
        _write_credentials(cred_file, {"client_id": "cid", "client_secret": "csecret", "refresh_token": "rtok"})

        captured = {}

        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return FakeResponse(200, json_data={"access_token": "AT123"})

        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        token = youtube_api.get_access_token(cred_file=str(cred_file))

        assert token == "AT123"
        assert captured["url"] == "https://oauth2.googleapis.com/token"
        assert captured["data"]["refresh_token"] == "rtok"
        assert captured["data"]["client_id"] == "cid"
        assert captured["data"]["grant_type"] == "refresh_token"

    def test_missing_file_raises_filenotfounderror(self, youtube_api, tmp_path):
        missing = tmp_path / "nope.json"

        with pytest.raises(FileNotFoundError):
            youtube_api.get_access_token(cred_file=str(missing))

    def test_incomplete_credentials_raise_valueerror(self, youtube_api, tmp_path):
        cred_file = tmp_path / "creds.json"
        _write_credentials(cred_file, {"client_id": "cid", "client_secret": "csecret"})  # refresh_token fehlt

        with pytest.raises(ValueError, match="unvollständig"):
            youtube_api.get_access_token(cred_file=str(cred_file))

    def test_non_200_token_response_raises_runtimeerror(self, youtube_api, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.json"
        _write_credentials(cred_file, {"client_id": "cid", "client_secret": "csecret", "refresh_token": "rtok"})
        monkeypatch.setattr(youtube_api.requests, "post", lambda *a, **k: FakeResponse(400, text="invalid_grant"))

        with pytest.raises(RuntimeError, match="Erneuern"):
            youtube_api.get_access_token(cred_file=str(cred_file))

    def test_client_secrets_file_overrides_client_id_and_secret_but_not_refresh_token(
        self, youtube_api, monkeypatch, tmp_path
    ):
        cred_file = tmp_path / "creds.json"
        _write_credentials(cred_file, {"client_id": "old_id", "client_secret": "old_secret", "refresh_token": "rtok"})
        secrets_file = tmp_path / "client_secrets.json"
        _write_credentials(secrets_file, {"installed": {"client_id": "new_id", "client_secret": "new_secret"}})

        captured = {}

        def fake_post(url, data=None, timeout=None):
            captured.update(data)
            return FakeResponse(200, json_data={"access_token": "AT"})

        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        youtube_api.get_access_token(cred_file=str(cred_file), client_secrets_file=str(secrets_file))

        assert captured["client_id"] == "new_id"
        assert captured["client_secret"] == "new_secret"
        # refresh_token kommt immer aus der Credentials-Datei, nie aus client_secrets_file
        assert captured["refresh_token"] == "rtok"

    def test_overly_permissive_credentials_file_gets_corrected_to_600(self, youtube_api, monkeypatch, tmp_path):
        cred_file = tmp_path / "creds.json"
        _write_credentials(
            cred_file, {"client_id": "cid", "client_secret": "csecret", "refresh_token": "rtok"}, mode=0o644
        )
        monkeypatch.setattr(youtube_api.requests, "post", lambda *a, **k: FakeResponse(200, json_data={"access_token": "AT"}))

        youtube_api.get_access_token(cred_file=str(cred_file))

        assert os.stat(cred_file).st_mode & 0o777 == 0o600


def _forbidden_get(*args, **kwargs):
    raise AssertionError("requests.get sollte nicht aufgerufen werden")


def _make_fake_get(responses):
    """Double für requests.get(): liefert der Reihe nach die übergebenen Antworten."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return responses.pop(0)

    fake_get.calls = calls
    return fake_get


def _make_fake_post(create_responses=None, item_responses=None):
    """
    Double für requests.post(): routet anhand der URL zwischen den beiden von
    add_video_to_playlist() genutzten Endpunkten (Playlist-Erstellung vs.
    Playlist-Item-Zuweisung), da beide über dieselbe requests.post()-Funktion laufen.
    """
    calls = []
    create_responses = list(create_responses or [])
    item_responses = list(item_responses or [])

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "json": json})
        if "playlistItems" in url:
            return item_responses.pop(0)
        return create_responses.pop(0)

    fake_post.calls = calls
    return fake_post


class TestAddVideoToPlaylist:
    def test_returns_false_without_playlist_name(self, youtube_api, monkeypatch):
        monkeypatch.setattr(youtube_api.requests, "get", _forbidden_get)

        result = youtube_api.add_video_to_playlist("vid1", "", "token")

        assert result is False

    def test_returns_false_without_video_id(self, youtube_api, monkeypatch):
        monkeypatch.setattr(youtube_api.requests, "get", _forbidden_get)

        result = youtube_api.add_video_to_playlist("", "Playlist", "token")

        assert result is False

    def test_existing_playlist_found_case_insensitively_and_item_added(self, youtube_api, monkeypatch):
        fake_get = _make_fake_get([
            FakeResponse(200, json_data={"items": [{"id": "pl1", "snippet": {"title": "My Playlist"}}]})
        ])
        monkeypatch.setattr(youtube_api.requests, "get", fake_get)
        fake_post = _make_fake_post(item_responses=[FakeResponse(201)])
        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        result = youtube_api.add_video_to_playlist("vid1", "my playlist", "tok")

        assert result is True
        assert len(fake_post.calls) == 1
        assert fake_post.calls[0]["json"]["snippet"]["playlistId"] == "pl1"
        assert fake_post.calls[0]["json"]["snippet"]["resourceId"]["videoId"] == "vid1"

    def test_playlist_search_paginates_until_found(self, youtube_api, monkeypatch):
        page1 = FakeResponse(200, json_data={
            "items": [{"id": "other", "snippet": {"title": "Other"}}],
            "nextPageToken": "PAGE2",
        })
        page2 = FakeResponse(200, json_data={"items": [{"id": "pl2", "snippet": {"title": "Target"}}]})
        fake_get = _make_fake_get([page1, page2])
        monkeypatch.setattr(youtube_api.requests, "get", fake_get)
        fake_post = _make_fake_post(item_responses=[FakeResponse(200)])
        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        result = youtube_api.add_video_to_playlist("vid2", "Target", "tok")

        assert result is True
        assert len(fake_get.calls) == 2
        assert "pageToken=PAGE2" in fake_get.calls[1]["url"]

    def test_playlist_not_found_gets_created_then_item_added(self, youtube_api, monkeypatch):
        fake_get = _make_fake_get([FakeResponse(200, json_data={"items": []})])
        monkeypatch.setattr(youtube_api.requests, "get", fake_get)
        fake_post = _make_fake_post(
            create_responses=[FakeResponse(201, json_data={"id": "new_pl"})],
            item_responses=[FakeResponse(200)],
        )
        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        result = youtube_api.add_video_to_playlist("vid3", "Brand New", "tok", privacy="unlisted")

        assert result is True
        create_call = fake_post.calls[0]
        assert create_call["json"]["snippet"]["title"] == "Brand New"
        assert create_call["json"]["status"]["privacyStatus"] == "unlisted"
        assert fake_post.calls[1]["json"]["snippet"]["playlistId"] == "new_pl"

    def test_401_on_playlist_list_triggers_token_refresh_and_retries(self, youtube_api, monkeypatch):
        fake_get = _make_fake_get([
            FakeResponse(401, text="expired"),
            FakeResponse(200, json_data={"items": [{"id": "pl9", "snippet": {"title": "Refreshed"}}]}),
        ])
        monkeypatch.setattr(youtube_api.requests, "get", fake_get)
        fake_post = _make_fake_post(item_responses=[FakeResponse(200)])
        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        refresh_calls = []
        monkeypatch.setattr(
            youtube_api, "get_access_token",
            lambda cred_file=None, client_secrets_file=None: refresh_calls.append(1) or "new-token"
        )

        result = youtube_api.add_video_to_playlist("vid4", "Refreshed", "old-token")

        assert result is True
        assert len(refresh_calls) == 1
        assert fake_get.calls[1]["headers"]["Authorization"] == "Bearer new-token"

    def test_playlist_creation_failure_returns_false_without_item_call(self, youtube_api, monkeypatch):
        fake_get = _make_fake_get([FakeResponse(200, json_data={"items": []})])
        monkeypatch.setattr(youtube_api.requests, "get", fake_get)
        fake_post = _make_fake_post(create_responses=[FakeResponse(500, text="server error")])
        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        result = youtube_api.add_video_to_playlist("vid5", "Failing", "tok")

        assert result is False
        assert len(fake_post.calls) == 1

    def test_item_add_retries_on_conflict_then_succeeds(self, youtube_api, monkeypatch):
        fake_get = _make_fake_get([
            FakeResponse(200, json_data={"items": [{"id": "pl7", "snippet": {"title": "Retry"}}]})
        ])
        monkeypatch.setattr(youtube_api.requests, "get", fake_get)
        fake_post = _make_fake_post(item_responses=[FakeResponse(409, text="conflict"), FakeResponse(200)])
        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        result = youtube_api.add_video_to_playlist("vid6", "Retry", "tok")

        assert result is True
        assert len(fake_post.calls) == 2

    def test_item_add_exhausts_retries_and_returns_false(self, youtube_api, monkeypatch):
        fake_get = _make_fake_get([
            FakeResponse(200, json_data={"items": [{"id": "pl8", "snippet": {"title": "Exhaust"}}]})
        ])
        monkeypatch.setattr(youtube_api.requests, "get", fake_get)
        fake_post = _make_fake_post(item_responses=[FakeResponse(500, text="x")] * 3)
        monkeypatch.setattr(youtube_api.requests, "post", fake_post)

        result = youtube_api.add_video_to_playlist("vid7", "Exhaust", "tok")

        assert result is False
        assert len(fake_post.calls) == 3

"""
Tests für die Chunk-Upload-Retry-Logik in upload_single_video().

Diese Tests mocken requests.Session komplett (kein echter Netzwerk-Call) und
get_access_token(), um gezielt jeden Zweig der Retry-Schleife durchzuspielen:
Erfolg, Fortsetzung (308), Token-Ablauf (401/403), dauerhafte Client-Fehler
(PermanentUploadError) und transiente Server-Fehler mit Backoff.

thumb_path=None und playlist_name=None werden überall übergeben, damit die
Post-Upload-Schritte (Thumbnail/Playlist) übersprungen werden und sich die
Tests ausschließlich auf die Chunk-Übertragung konzentrieren.
"""

import pytest


class FakeResponse:
    """Minimales Double für requests.Response - nur was der Code tatsächlich nutzt."""

    def __init__(self, status_code, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json_data


class FakeSession:
    """
    Double für requests.Session. init-POST liefert immer dieselbe Antwort,
    PUT-Aufrufe (Chunks) liefern der Reihe nach die in put_responses angegebenen
    Antworten. Zeichnet zusätzlich die verwendeten Header pro PUT-Aufruf auf,
    um z.B. den Authorization-Header nach einem Token-Refresh zu prüfen.
    """

    def __init__(self, init_response, put_responses):
        self._init_response = init_response
        self._put_responses = list(put_responses)
        self.put_calls = 0
        self.put_headers_seen = []

    def post(self, url, headers=None, json=None, timeout=None):
        return self._init_response

    def put(self, url, headers=None, data=None, timeout=None):
        self.put_calls += 1
        self.put_headers_seen.append(dict(headers or {}))
        return self._put_responses.pop(0)


@pytest.fixture
def video_file(tmp_path):
    """Kleine Testdatei für Single-Chunk-Szenarien (Inhalt ist irrelevant)."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 10)
    return str(path)


@pytest.fixture
def large_video_file(tmp_path):
    """
    Datei größer als eine CHUNK_UNIT_BYTES-Einheit (256 KiB), damit echte
    Mehr-Chunk-Szenarien (308 -> weiterer Chunk -> 200) getestet werden können.
    """
    path = tmp_path / "video_large.mp4"
    path.write_bytes(b"x" * 300_000)
    return str(path)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch, youtube_api):
    """Verhindert echte time.sleep()-Aufrufe während der Retry-Tests (2**attempt Sekunden)."""
    monkeypatch.setattr(youtube_api.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def stub_access_token(monkeypatch, youtube_api):
    """Ersetzt get_access_token() standardmäßig durch einen festen Dummy-Token."""
    monkeypatch.setattr(youtube_api, "get_access_token", lambda cred_file=None, client_secrets_file=None: "fake-token")


def _install_fake_session(monkeypatch, youtube_api, fake_session):
    monkeypatch.setattr(youtube_api.requests, "Session", lambda: fake_session)


class TestChunkUploadSuccess:
    def test_single_chunk_success_returns_video_id(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session1"})
        put_response = FakeResponse(200, json_data={"id": "vid_success"})
        fake_session = FakeSession(init_response, [put_response])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        result = youtube_api.upload_single_video(
            file_path=video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None
        )

        assert result == "vid_success"
        assert fake_session.put_calls == 1

    def test_multi_chunk_upload_continues_after_308(self, youtube_api, monkeypatch, large_video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-multi"})
        put_308 = FakeResponse(308, headers={"Range": "bytes=0-262143"})
        put_200 = FakeResponse(200, json_data={"id": "vid_multi_chunk"})
        fake_session = FakeSession(init_response, [put_308, put_200])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        result = youtube_api.upload_single_video(
            file_path=large_video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None,
            chunksize=262144
        )

        assert result == "vid_multi_chunk"
        assert fake_session.put_calls == 2
        assert fake_session.put_headers_seen[0]["Content-Range"] == "bytes 0-262143/300000"
        assert fake_session.put_headers_seen[1]["Content-Range"] == "bytes 262144-299999/300000"

    def test_interactive_tty_shows_both_progress_bar_and_log_line(self, youtube_api, monkeypatch, large_video_file, capsys, caplog):
        """
        Bei interaktivem stderr (echtes Terminal) läuft zusätzlich zur
        "Fortschritt: ..."-Logzeile der Live-Balken mit. Die Logzeile darf NICHT
        entfallen, nur weil isatty() True liefert - Container mit `tty: true`
        (z.B. für Podman-Kompatibilität gesetzt) melden isatty()=True auch im
        unbeaufsichtigten Daemon-Betrieb, wo der \\r-Balken (schreibt direkt auf
        stderr, nie über den Logger) sonst der einzige Fortschrittsindikator
        wäre und in `docker logs` nie auftauchen würde.
        """
        monkeypatch.setattr(youtube_api.sys.stderr, "isatty", lambda: True)
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-tty"})
        put_308 = FakeResponse(308, headers={"Range": "bytes=0-262143"})
        put_200 = FakeResponse(200, json_data={"id": "vid_tty"})
        fake_session = FakeSession(init_response, [put_308, put_200])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        with caplog.at_level("INFO"):
            result = youtube_api.upload_single_video(
                file_path=large_video_file, title="Test", desc="", category=None, tags=None,
                rec_date=None, thumb_path=None, playlist_name=None,
                chunksize=262144
            )

        assert result == "vid_tty"
        assert any("Fortschritt:" in record.message for record in caplog.records)
        stderr_output = capsys.readouterr().err
        assert "%" in stderr_output
        assert "100.0%" in stderr_output


class TestChunkUploadTokenRefresh:
    def test_401_triggers_refresh_and_retries_successfully(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session2"})
        put_401 = FakeResponse(401, text="unauthorized")
        put_ok = FakeResponse(200, json_data={"id": "vid_after_refresh"})
        fake_session = FakeSession(init_response, [put_401, put_ok])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        token_calls = []
        monkeypatch.setattr(
            youtube_api, "get_access_token",
            lambda cred_file=None, client_secrets_file=None: token_calls.append(1) or "refreshed-token"
        )

        result = youtube_api.upload_single_video(
            file_path=video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None
        )

        assert result == "vid_after_refresh"
        assert fake_session.put_calls == 2
        assert len(token_calls) >= 2  # initialer Token + mind. 1 Refresh

    def test_retry_after_401_uses_the_new_token(self, youtube_api, monkeypatch, large_video_file):
        """Stellt sicher, dass der Retry-Header wirklich den NEUEN Token trägt, nicht den alten."""
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-token"})
        put_401 = FakeResponse(401, text="unauthorized")
        put_ok = FakeResponse(200, json_data={"id": "vid_token_check"})
        fake_session = FakeSession(init_response, [put_401, put_ok])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        tokens = iter(["token-A", "token-B", "token-C", "token-D"])
        monkeypatch.setattr(
            youtube_api, "get_access_token",
            lambda cred_file=None, client_secrets_file=None: next(tokens)
        )

        youtube_api.upload_single_video(
            file_path=large_video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None,
            chunksize=262144
        )

        auth_headers = [h["Authorization"] for h in fake_session.put_headers_seen]
        assert auth_headers[0] == "Bearer token-A"
        assert auth_headers[1] == "Bearer token-B"
        assert auth_headers[0] != auth_headers[1]


class TestChunkUploadErrorHandling:
    def test_permanent_client_error_aborts_without_retry(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session3"})
        put_400 = FakeResponse(400, text="bad request")
        # 5 identische Antworten bereitstellen, damit ein Test-Fehlschlag (kein Abbruch)
        # nicht an einem IndexError scheitert, sondern am fehlenden PermanentUploadError
        fake_session = FakeSession(init_response, [put_400] * 5)
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        with pytest.raises(youtube_api.PermanentUploadError):
            youtube_api.upload_single_video(
                file_path=video_file, title="Test", desc="", category=None, tags=None,
                rec_date=None, thumb_path=None, playlist_name=None
            )

        # Kernpunkt des Fixes: bei einem dauerhaften Fehler wird NICHT 5x retried
        assert fake_session.put_calls == 1

    def test_transient_server_error_retries_then_succeeds(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session4"})
        put_503 = FakeResponse(503, text="service unavailable")
        put_ok = FakeResponse(200, json_data={"id": "vid_after_503"})
        fake_session = FakeSession(init_response, [put_503, put_ok])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        result = youtube_api.upload_single_video(
            file_path=video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None
        )

        assert result == "vid_after_503"
        assert fake_session.put_calls == 2

    def test_max_retries_exceeded_raises_runtime_error(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session5"})
        fake_session = FakeSession(init_response, [FakeResponse(503, text="x")] * 5)
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        with pytest.raises(RuntimeError, match="Max Retries"):
            youtube_api.upload_single_video(
                file_path=video_file, title="Test", desc="", category=None, tags=None,
                rec_date=None, thumb_path=None, playlist_name=None
            )

        assert fake_session.put_calls == 5

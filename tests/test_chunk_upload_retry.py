"""
Tests für die Chunk-Upload-Retry-Logik in upload_single_video().

Diese Tests mocken requests.Session komplett (kein echter Netzwerk-Call) und
get_access_token(), um gezielt jeden Zweig der Retry-Schleife durchzuspielen:
Erfolg, Fortsetzung (308), Token-Ablauf (401), 403 (Rate-Limit vs. dauerhaft
je nach `reason`), dauerhafte Client-Fehler (PermanentUploadError) und
transiente Server-Fehler mit Backoff.

thumb_path=None und playlist_name=None werden überall übergeben, damit die
Post-Upload-Schritte (Thumbnail/Playlist) übersprungen werden und sich die
Tests ausschließlich auf die Chunk-Übertragung konzentrieren.
"""

import json

import pytest


def _error_body(reason, message="details"):
    """Baut den JSON-Fehlertext im Standard-Google-API-Format (siehe _parse_api_error)."""
    return json.dumps({"error": {"errors": [{"reason": reason}], "message": message}})


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
    Double für requests.Session. init-POST liefert immer dieselbe Antwort (oder,
    falls eine Liste übergeben wird, der Reihe nach jeweils die nächste - für
    Tests der Session-Init-Retry-Schleife). PUT-Aufrufe (Chunks) liefern der
    Reihe nach die in put_responses angegebenen Antworten. Zeichnet zusätzlich
    die verwendeten Header pro PUT-Aufruf auf, um z.B. den Authorization-Header
    nach einem Token-Refresh zu prüfen.
    """

    def __init__(self, init_response, put_responses):
        self._init_responses = init_response if isinstance(init_response, list) else None
        self._init_response = None if self._init_responses else init_response
        self._put_responses = list(put_responses)
        self.put_calls = 0
        self.put_headers_seen = []

    def post(self, url, headers=None, json=None, timeout=None):
        if self._init_responses:
            return self._init_responses.pop(0)
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


class TestSessionInit403Handling:
    """Dieselbe reason-basierte 403-Unterscheidung gilt auch für die Session-Init-Retry-Schleife."""

    def test_rate_limit_reason_retries_session_init_and_succeeds(self, youtube_api, monkeypatch, video_file):
        init_403 = FakeResponse(403, text=_error_body("rateLimitExceeded"))
        init_ok = FakeResponse(200, headers={"Location": "https://fake/session-init-rate"})
        put_ok = FakeResponse(200, json_data={"id": "vid_init_after_rate_limit"})
        fake_session = FakeSession([init_403, init_ok], [put_ok])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        result = youtube_api.upload_single_video(
            file_path=video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None
        )

        assert result == "vid_init_after_rate_limit"

    def test_quota_exceeded_aborts_session_init_immediately(self, youtube_api, monkeypatch, video_file):
        init_403 = FakeResponse(403, text=_error_body("quotaExceeded", "Daily quota exceeded"))
        fake_session = FakeSession([init_403] * 3, [])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        with pytest.raises(youtube_api.PermanentUploadError, match="quotaExceeded"):
            youtube_api.upload_single_video(
                file_path=video_file, title="Test", desc="", category=None, tags=None,
                rec_date=None, thumb_path=None, playlist_name=None
            )


class TestChunkUpload403Handling:
    """
    403 wurde frueher wie 401 behandelt (Token-Refresh + Retry) - das hilft aber
    nicht bei quotaExceeded/forbidden/insufficientPermissions etc., die sich
    durch einen neuen Token nicht loesen. Nur die beiden von Googles eigenem
    google-api-python-client als retry-wuerdig eingestuften reasons
    (userRateLimitExceeded/rateLimitExceeded) sollen erneut versucht werden,
    alles andere muss sofort als PermanentUploadError durchschlagen.
    """

    def test_rate_limit_reason_retries_and_succeeds(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-403-rate"})
        put_403 = FakeResponse(403, text=_error_body("userRateLimitExceeded"))
        put_ok = FakeResponse(200, json_data={"id": "vid_after_rate_limit"})
        fake_session = FakeSession(init_response, [put_403, put_ok])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        result = youtube_api.upload_single_video(
            file_path=video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None
        )

        assert result == "vid_after_rate_limit"
        assert fake_session.put_calls == 2

    def test_quota_exceeded_aborts_immediately_without_retry(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-403-quota"})
        put_403 = FakeResponse(403, text=_error_body("quotaExceeded", "Daily quota exceeded"))
        fake_session = FakeSession(init_response, [put_403] * 5)
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        with pytest.raises(youtube_api.PermanentUploadError, match="quotaExceeded"):
            youtube_api.upload_single_video(
                file_path=video_file, title="Test", desc="", category=None, tags=None,
                rec_date=None, thumb_path=None, playlist_name=None
            )

        # Kernpunkt des Fixes: quotaExceeded loest sich nicht durch Token-Refresh,
        # also darf hier NICHT 5x sinnlos retried werden.
        assert fake_session.put_calls == 1

    def test_unparseable_403_body_still_aborts_as_permanent(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-403-broken"})
        put_403 = FakeResponse(403, text="not json")
        fake_session = FakeSession(init_response, [put_403] * 5)
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        with pytest.raises(youtube_api.PermanentUploadError):
            youtube_api.upload_single_video(
                file_path=video_file, title="Test", desc="", category=None, tags=None,
                rec_date=None, thumb_path=None, playlist_name=None
            )

        assert fake_session.put_calls == 1


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


class TestChunkUploadContentRangeProbe:
    """
    "Failed to parse Content-Range header" ist ein bekannter, gelegentlich
    transienter Ausrutscher der YouTube-API (v.a. beim letzten Chunk sehr
    grosser Uploads) - statt das wie einen dauerhaften Fehler zu behandeln
    und die ganze Datei zu verwerfen, fragt der Code per Status-Check
    (leerer PUT mit `Content-Range: bytes */{file_size}`) den tatsaechlichen
    Serverstand ab und macht von dort weiter.
    """

    def test_probe_reveals_chunk_was_received_and_upload_resumes(self, youtube_api, monkeypatch, large_video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-probe"})
        put_400 = FakeResponse(400, text="Failed to parse Content-Range header")
        probe_308 = FakeResponse(308, headers={"Range": "bytes=0-262143"})
        put_ok = FakeResponse(200, json_data={"id": "vid_resumed"})
        fake_session = FakeSession(init_response, [put_400, probe_308, put_ok])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        result = youtube_api.upload_single_video(
            file_path=large_video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None,
            chunksize=262144
        )

        assert result == "vid_resumed"
        assert fake_session.put_calls == 3
        assert fake_session.put_headers_seen[1]["Content-Range"] == "bytes */300000"
        # Nach dem Status-Check muss der naechste Chunk ab dem PROBIERTEN Offset
        # weitergehen (262144), nicht erneut bei 0 anfangen.
        assert fake_session.put_headers_seen[2]["Content-Range"] == "bytes 262144-299999/300000"

    def test_probe_reveals_upload_was_already_complete(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-probe-done"})
        put_400 = FakeResponse(400, text="Failed to parse Content-Range header")
        probe_done = FakeResponse(200, json_data={"id": "vid_already_done"})
        fake_session = FakeSession(init_response, [put_400, probe_done])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        result = youtube_api.upload_single_video(
            file_path=video_file, title="Test", desc="", category=None, tags=None,
            rec_date=None, thumb_path=None, playlist_name=None
        )

        assert result == "vid_already_done"
        assert fake_session.put_calls == 2

    def test_probe_failure_falls_back_to_permanent_error(self, youtube_api, monkeypatch, video_file):
        init_response = FakeResponse(200, headers={"Location": "https://fake/session-probe-fail"})
        put_400 = FakeResponse(400, text="Failed to parse Content-Range header")
        probe_404 = FakeResponse(404, text="session gone")
        fake_session = FakeSession(init_response, [put_400, probe_404])
        _install_fake_session(monkeypatch, youtube_api, fake_session)

        with pytest.raises(youtube_api.PermanentUploadError):
            youtube_api.upload_single_video(
                file_path=video_file, title="Test", desc="", category=None, tags=None,
                rec_date=None, thumb_path=None, playlist_name=None
            )

        # Kein weiterer Retry-Versuch des eigentlichen Chunks nach dem gescheiterten Status-Check
        assert fake_session.put_calls == 2

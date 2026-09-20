"""
Tests für process_single_file(manage_files=True) (Auto-Batch/Dämon-Modus):
ein QuotaExceededError (tägliches YouTube-API-Kontingent oder Kanal-Upload-
Limit erreicht) ist kein Problem mit der Videodatei selbst und muss deshalb
nach RETRY_DIR statt CORRUPT_DIR verschoben werden, auch wenn noch kein
Segment erfolgreich hochgeladen wurde (progress ist leer). Ein generischer
Fehler ohne jeden Fortschritt landet weiterhin in CORRUPT_DIR.
"""

DEFAULT_META = {
    "title": "Titel",
    "description": "Beschreibung",
    "purl": None,
    "genre": None,
    "date": None,
    "artist": None,
    "thumb_path": None,
    "duration": 100,
}


def _patch_common(monkeypatch, pipeline, upload_side_effect):
    monkeypatch.setattr(pipeline, "is_file_ready_and_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(pipeline, "extract_metadata_and_thumb", lambda *_a, **_k: dict(DEFAULT_META))
    monkeypatch.setattr(pipeline, "split_video_if_needed", lambda work_path, output_dir=None: [work_path])
    monkeypatch.setattr(pipeline, "upload_single_video", upload_side_effect)


def _setup_dirs(config, monkeypatch, tmp_path):
    work_dir = tmp_path / "work"
    corrupt_dir = tmp_path / "corrupt"
    retry_dir = tmp_path / "retry"
    for d in (work_dir, corrupt_dir, retry_dir):
        d.mkdir()
    monkeypatch.setattr(config, "WORK_DIR", str(work_dir))
    monkeypatch.setattr(config, "CORRUPT_DIR", str(corrupt_dir))
    monkeypatch.setattr(config, "RETRY_DIR", str(retry_dir))
    return corrupt_dir, retry_dir


class TestQuotaExceededRouting:
    def test_quota_exceeded_with_no_progress_goes_to_retry_not_corrupt(
        self, pipeline, config, tmp_path, monkeypatch, youtube_api
    ):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake video content")
        corrupt_dir, retry_dir = _setup_dirs(config, monkeypatch, tmp_path)

        def failing_upload(**_k):
            raise youtube_api.QuotaExceededError("Nicht behebbarer 403-Fehler (quotaExceeded): Daily quota exceeded")

        _patch_common(monkeypatch, pipeline, failing_upload)

        pipeline.process_single_file(str(video))

        assert not video.exists()
        assert list(corrupt_dir.iterdir()) == []
        assert [p.name for p in retry_dir.iterdir()] == ["video.mp4"]

    def test_generic_permanent_error_with_no_progress_still_goes_to_corrupt(
        self, pipeline, config, tmp_path, monkeypatch, youtube_api
    ):
        """Regressionsschutz: nur QuotaExceededError bekommt die RETRY-Sonderbehandlung."""
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake video content")
        corrupt_dir, retry_dir = _setup_dirs(config, monkeypatch, tmp_path)

        def failing_upload(**_k):
            raise youtube_api.PermanentUploadError("Nicht behebbarer Fehler (400): invalidVideoMetadata")

        _patch_common(monkeypatch, pipeline, failing_upload)

        pipeline.process_single_file(str(video))

        assert not video.exists()
        assert [p.name for p in corrupt_dir.iterdir()] == ["video.mp4"]
        assert list(retry_dir.iterdir()) == []

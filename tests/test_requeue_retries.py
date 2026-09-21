"""
Tests für fileutils.requeue_retries(): verschiebt Dateien aus RETRY_DIR
zurück nach IN_DIR für den periodischen systemd-Timer (yt-upload-retry.timer).
Bewusst kein eigener Cooldown im Code (siehe Docstring der Funktion) - diese
Tests decken daher nur das reine Verschieben/Kollisions-/Fehlerverhalten ab.
"""


def _setup_dirs(config, monkeypatch, tmp_path):
    retry_dir = tmp_path / "retry"
    in_dir = tmp_path / "in"
    retry_dir.mkdir()
    in_dir.mkdir()
    monkeypatch.setattr(config, "RETRY_DIR", str(retry_dir))
    monkeypatch.setattr(config, "IN_DIR", str(in_dir))
    return retry_dir, in_dir


class TestRequeueRetries:
    def test_missing_retry_dir_returns_zero(self, fileutils, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "RETRY_DIR", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(config, "IN_DIR", str(tmp_path / "in"))

        assert fileutils.requeue_retries() == 0

    def test_empty_retry_dir_returns_zero(self, fileutils, config, monkeypatch, tmp_path):
        retry_dir, _ = _setup_dirs(config, monkeypatch, tmp_path)

        assert fileutils.requeue_retries() == 0

    def test_single_file_moved_to_in_dir(self, fileutils, config, monkeypatch, tmp_path):
        retry_dir, in_dir = _setup_dirs(config, monkeypatch, tmp_path)
        (retry_dir / "video.mkv").write_bytes(b"content")

        moved = fileutils.requeue_retries()

        assert moved == 1
        assert not (retry_dir / "video.mkv").exists()
        assert (in_dir / "video.mkv").read_bytes() == b"content"

    def test_multiple_files_all_moved(self, fileutils, config, monkeypatch, tmp_path):
        retry_dir, in_dir = _setup_dirs(config, monkeypatch, tmp_path)
        (retry_dir / "a.mkv").write_bytes(b"a")
        (retry_dir / "b.mp4").write_bytes(b"b")

        moved = fileutils.requeue_retries()

        assert moved == 2
        assert list(retry_dir.iterdir()) == []
        assert {p.name for p in in_dir.iterdir()} == {"a.mkv", "b.mp4"}

    def test_name_collision_in_in_dir_is_skipped_not_overwritten(self, fileutils, config, monkeypatch, tmp_path, caplog):
        retry_dir, in_dir = _setup_dirs(config, monkeypatch, tmp_path)
        (retry_dir / "video.mkv").write_bytes(b"retry-content")
        (in_dir / "video.mkv").write_bytes(b"already-here")

        with caplog.at_level("WARNING"):
            moved = fileutils.requeue_retries()

        assert moved == 0
        # Original in RETRY_DIR bleibt unangetastet, IN_DIR-Datei wird nicht überschrieben
        assert (retry_dir / "video.mkv").read_bytes() == b"retry-content"
        assert (in_dir / "video.mkv").read_bytes() == b"already-here"
        assert any("Namenskollision" in r.message for r in caplog.records)

    def test_hidden_sidecar_style_file_is_ignored(self, fileutils, config, monkeypatch, tmp_path):
        """Vorsichtsmaßnahme: RETRY_DIR sollte nie versteckte Dateien enthalten,
        aber requeue_retries() soll sie trotzdem nicht anfassen, falls doch."""
        retry_dir, in_dir = _setup_dirs(config, monkeypatch, tmp_path)
        (retry_dir / ".video.mkv.progress.json").write_text("{}")

        moved = fileutils.requeue_retries()

        assert moved == 0
        assert (retry_dir / ".video.mkv.progress.json").exists()
        assert list(in_dir.iterdir()) == []

    def test_subdirectory_in_retry_dir_is_ignored(self, fileutils, config, monkeypatch, tmp_path):
        retry_dir, in_dir = _setup_dirs(config, monkeypatch, tmp_path)
        (retry_dir / "some_subdir").mkdir()

        moved = fileutils.requeue_retries()

        assert moved == 0
        assert (retry_dir / "some_subdir").is_dir()
        assert list(in_dir.iterdir()) == []

    def test_partial_failure_does_not_stop_remaining_files(self, fileutils, config, monkeypatch, tmp_path):
        """Ein fehlschlagender shutil.move() für eine Datei darf die anderen nicht blockieren."""
        retry_dir, in_dir = _setup_dirs(config, monkeypatch, tmp_path)
        (retry_dir / "a.mkv").write_bytes(b"a")
        (retry_dir / "b.mkv").write_bytes(b"b")

        real_move = fileutils.shutil.move

        def flaky_move(src, dst):
            if "a.mkv" in src:
                raise OSError("simulated failure")
            return real_move(src, dst)

        monkeypatch.setattr(fileutils.shutil, "move", flaky_move)

        moved = fileutils.requeue_retries()

        assert moved == 1
        assert (retry_dir / "a.mkv").exists()
        assert (in_dir / "b.mkv").exists()

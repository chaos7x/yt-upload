"""
Tests für die Symlink-Ablehnung in process_single_file() (pipeline.py).

os.path.exists()/is_file_ready_and_valid() (ffprobe) folgen Symlinks
transparent; ohne eine explizite islink()-Prüfung ganz am Anfang von
process_single_file() würde ein Symlink mit erlaubter Endung in IN_DIR
(z.B. "video.mp4" -> eine beliebige lesbare Datei) dazu führen, dass der
Inhalt der Zieldatei öffentlich zu YouTube hochgeladen wird.
"""

import os


class TestProcessSingleFileSymlinkRejection:
    def test_symlink_is_quarantined_without_touching_target(self, pipeline, config, tmp_path, monkeypatch):
        in_dir = tmp_path / "in"
        corrupt_dir = tmp_path / "corrupt"
        in_dir.mkdir()
        corrupt_dir.mkdir()

        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")

        link = in_dir / "evil.mp4"
        link.symlink_to(secret)

        monkeypatch.setattr(config, "CORRUPT_DIR", corrupt_dir)
        monkeypatch.setattr(config, "ALLOW_OVERWRITE", True)

        def _forbidden(*a, **k):
            raise AssertionError("is_file_ready_and_valid() sollte für Symlinks nie aufgerufen werden")

        monkeypatch.setattr(pipeline, "is_file_ready_and_valid", _forbidden)

        pipeline.process_single_file(str(link))

        quarantined = corrupt_dir / "evil.mp4"
        assert quarantined.is_symlink()
        assert os.readlink(quarantined) == str(secret)
        assert not link.exists()
        # Das Linkziel selbst darf nie gelesen/verändert worden sein
        assert secret.read_text() == "TOP SECRET"

    def test_symlink_quarantine_falls_back_across_filesystems_without_dereferencing(
        self, pipeline, config, tmp_path, monkeypatch
    ):
        """Simuliert IN_DIR/CORRUPT_DIR auf unterschiedlichen Mounts (os.rename -> EXDEV)."""
        in_dir = tmp_path / "in"
        corrupt_dir = tmp_path / "corrupt"
        in_dir.mkdir()
        corrupt_dir.mkdir()

        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")

        link = in_dir / "evil.mp4"
        link.symlink_to(secret)

        monkeypatch.setattr(config, "CORRUPT_DIR", corrupt_dir)
        monkeypatch.setattr(config, "ALLOW_OVERWRITE", True)
        monkeypatch.setattr(pipeline, "is_file_ready_and_valid", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

        real_rename = pipeline.os.rename

        def fake_rename(src, dst):
            raise OSError("simulated EXDEV: cross-device link")

        monkeypatch.setattr(pipeline.os, "rename", fake_rename)

        pipeline.process_single_file(str(link))

        quarantined = corrupt_dir / "evil.mp4"
        assert quarantined.is_symlink()
        assert os.readlink(quarantined) == str(secret)
        assert not link.exists()
        assert secret.read_text() == "TOP SECRET"

        monkeypatch.setattr(pipeline.os, "rename", real_rename)

    def test_regular_file_is_not_quarantined_by_the_symlink_check(self, pipeline, config, tmp_path, monkeypatch):
        """Regressionsschutz: normale Dateien durchlaufen weiterhin is_file_ready_and_valid()."""
        in_dir = tmp_path / "in"
        corrupt_dir = tmp_path / "corrupt"
        in_dir.mkdir()
        corrupt_dir.mkdir()

        video = in_dir / "video.mp4"
        video.write_bytes(b"fake video content")

        monkeypatch.setattr(config, "CORRUPT_DIR", corrupt_dir)
        monkeypatch.setattr(config, "ALLOW_OVERWRITE", True)

        called = {"n": 0}

        def fake_ready_check(*a, **k):
            called["n"] += 1
            return False  # führt zum bereits bestehenden CORRUPT-Pfad, nicht Teil dieses Fixes

        monkeypatch.setattr(pipeline, "is_file_ready_and_valid", fake_ready_check)

        pipeline.process_single_file(str(video))

        assert called["n"] == 1

"""Tests für resolve_target_path, unique_path und _progress_file_path."""

import os


class TestUniquePath:
    def test_returns_candidate_if_no_collision(self, fileutils, tmp_path):
        result = fileutils.unique_path(str(tmp_path), "video.mp4")
        assert result == str(tmp_path / "video.mp4")

    def test_appends_counter_on_single_collision(self, fileutils, tmp_path):
        (tmp_path / "video.mp4").write_bytes(b"x")

        result = fileutils.unique_path(str(tmp_path), "video.mp4")
        assert result == str(tmp_path / "video_1.mp4")

    def test_increments_counter_past_multiple_collisions(self, fileutils, tmp_path):
        (tmp_path / "video.mp4").write_bytes(b"x")
        (tmp_path / "video_1.mp4").write_bytes(b"x")
        (tmp_path / "video_2.mp4").write_bytes(b"x")

        result = fileutils.unique_path(str(tmp_path), "video.mp4")
        assert result == str(tmp_path / "video_3.mp4")

    def test_preserves_file_extension(self, fileutils, tmp_path):
        (tmp_path / "clip.mkv").write_bytes(b"x")

        result = fileutils.unique_path(str(tmp_path), "clip.mkv")
        assert result.endswith("clip_1.mkv")


class TestResolveTargetPath:
    def test_overwrite_true_returns_plain_candidate_even_if_exists(self, fileutils, config, tmp_path, monkeypatch):
        # ALLOW_OVERWRITE lebt in config.py - resolve_target_path (in fileutils.py)
        # liest es zur Laufzeit als config.ALLOW_OVERWRITE, daher muss hier auf
        # dem config-Modul gepatcht werden, nicht auf fileutils.
        monkeypatch.setattr(config, "ALLOW_OVERWRITE", True)
        (tmp_path / "video.mp4").write_bytes(b"x")

        result = fileutils.resolve_target_path(str(tmp_path), "video.mp4")
        assert result == str(tmp_path / "video.mp4")

    def test_overwrite_false_avoids_existing_file(self, fileutils, config, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "ALLOW_OVERWRITE", False)
        (tmp_path / "video.mp4").write_bytes(b"x")

        result = fileutils.resolve_target_path(str(tmp_path), "video.mp4")
        assert result == str(tmp_path / "video_1.mp4")

    def test_overwrite_false_no_collision_returns_plain_candidate(self, fileutils, config, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "ALLOW_OVERWRITE", False)

        result = fileutils.resolve_target_path(str(tmp_path), "new.mp4")
        assert result == str(tmp_path / "new.mp4")


class TestProgressFilePath:
    def test_uses_dot_prefixed_sidecar_name_in_same_directory(self, fileutils, tmp_path):
        work_path = os.path.join(str(tmp_path), "video.mkv")

        result = fileutils._progress_file_path(work_path)
        assert result == os.path.join(str(tmp_path), ".video.mkv.progress.json")

    def test_handles_filename_without_directory_component(self, fileutils):
        # work_path ohne Pfadanteil (nur Dateiname) darf nicht crashen
        result = fileutils._progress_file_path("video.mkv")
        assert result == ".video.mkv.progress.json"

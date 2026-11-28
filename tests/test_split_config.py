"""
Tests für split_hours / SPLIT_HOURS (einstellbare Split-Länge) und dafür, dass
ein Retry nach Teil-Upload mit der ursprünglichen Split-Länge weiterarbeitet.
"""

import json
import shutil
import subprocess

import pytest


def _load(config, tmp_path, monkeypatch, content, env=None):
    path = tmp_path / "upload.conf"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "CONF_PATH", str(path))
    monkeypatch.setattr(config, "CONF_D_DIR", "")
    monkeypatch.delenv("SPLIT_HOURS", raising=False)
    if env is not None:
        monkeypatch.setenv("SPLIT_HOURS", env)
    config.load_configuration()


class TestSplitHoursConfig:
    def test_default_is_three_and_a_half_hours(self, config, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", config._DEFAULTS["SEGMENT_TIME_SEC"])
        _load(config, tmp_path, monkeypatch, "[settings]\n")
        assert config.SEGMENT_TIME_SEC == 12600

    def test_config_value(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[settings]\nsplit_hours = 4\n")
        assert config.SEGMENT_TIME_SEC == 4 * 3600

    @pytest.mark.parametrize("value", ["2.5", "2,5"])
    def test_fractional_hours(self, config, tmp_path, monkeypatch, value):
        _load(config, tmp_path, monkeypatch, f"[settings]\nsplit_hours = {value}\n")
        assert config.SEGMENT_TIME_SEC == 9000

    def test_env_overrides_config(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[settings]\nsplit_hours = 4\n", env="6")
        assert config.SEGMENT_TIME_SEC == 6 * 3600

    def test_zero_disables(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[settings]\nsplit_hours = 0\n")
        assert config.SEGMENT_TIME_SEC == 0

    @pytest.mark.parametrize("bad", ["viele", "-1", "nan", "inf"])
    def test_invalid_values_keep_previous(self, config, tmp_path, monkeypatch, bad):
        _load(config, tmp_path, monkeypatch, "[settings]\nsplit_hours = 3\n")
        _load(config, tmp_path, monkeypatch, f"[settings]\nsplit_hours = {bad}\n", env=bad)
        assert config.SEGMENT_TIME_SEC == 3 * 3600

    def test_removed_from_config_on_hot_reload_keeps_value(self, config, tmp_path, monkeypatch):
        # Wie alle [settings]-Werte: Hot-Reload ohne den Schlüssel ändert nichts
        _load(config, tmp_path, monkeypatch, "[settings]\nsplit_hours = 3\n")
        _load(config, tmp_path, monkeypatch, "[settings]\n")
        assert config.SEGMENT_TIME_SEC == 3 * 3600


def _probe_result(duration):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{duration}\n", stderr="")


class TestSplitVideoIfNeeded:
    def test_zero_never_splits(self, media, config, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(media.subprocess, "run", lambda cmd, **k: calls.append(cmd) or _probe_result(50 * 3600))
        video = tmp_path / "long.mkv"
        assert media.split_video_if_needed(str(video), output_dir=str(tmp_path), segment_time_sec=0) == [str(video)]
        assert len(calls) == 1  # nur ffprobe, kein ffmpeg

    def test_uses_config_value_by_default(self, media, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", 4 * 3600)
        calls = []

        def fake_run(cmd, **_k):
            calls.append(cmd)
            if cmd[0] == "ffmpeg":
                (tmp_path / "long_part00.mkv").write_bytes(b"x")
                (tmp_path / "long_part01.mkv").write_bytes(b"x")
            return _probe_result(5 * 3600)

        monkeypatch.setattr(media.subprocess, "run", fake_run)
        segments = media.split_video_if_needed(str(tmp_path / "long.mkv"), output_dir=str(tmp_path))
        assert len(segments) == 2
        ffmpeg_cmd = calls[1]
        assert ffmpeg_cmd[ffmpeg_cmd.index("-segment_time") + 1] == str(4 * 3600)

    def test_shorter_than_split_length_is_not_split(self, media, monkeypatch, tmp_path):
        monkeypatch.setattr(media.subprocess, "run", lambda cmd, **k: _probe_result(3 * 3600))
        video = tmp_path / "v.mkv"
        assert media.split_video_if_needed(str(video), output_dir=str(tmp_path), segment_time_sec=4 * 3600) == [str(video)]

    @pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe fehlt")
    def test_real_split_with_configured_length(self, media, config, monkeypatch, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        video = work / "clip.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=64x64:rate=10",
             "-g", "10", "-c:v", "mpeg4", str(video)],
            check=True,
        )
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", 2)
        segments = media.split_video_if_needed(str(video), output_dir=str(tmp_path))
        assert len(segments) == 3


class TestProgressSegmentTime:
    def test_no_progress_returns_none(self, fileutils, tmp_path):
        assert fileutils.load_progress_segment_time(str(tmp_path / "v.mkv")) is None

    def test_roundtrip_and_hidden_from_progress(self, fileutils, tmp_path):
        work = str(tmp_path / "v.mkv")
        fileutils.save_segment_progress(work, {"v_part00.mkv": "id1"}, 4 * 3600)
        assert fileutils.load_progress_segment_time(work) == 4 * 3600
        assert fileutils.load_segment_progress(work) == {"v_part00.mkv": "id1"}

    def test_legacy_progress_file_means_ten_hours(self, fileutils, tmp_path):
        (tmp_path / ".v.mkv.progress.json").write_text(json.dumps({"v_part00.mkv": "id1"}), encoding="utf-8")
        assert fileutils.load_progress_segment_time(str(tmp_path / "v.mkv")) == 36000


class TestPipelineUsesStoredSplitLength:
    def _run(self, pipeline, monkeypatch, tmp_path):
        video = tmp_path / "video.mkv"
        video.write_bytes(b"fake")
        seen = {}
        monkeypatch.setattr(pipeline, "is_file_ready_and_valid", lambda *_a, **_k: True)
        monkeypatch.setattr(pipeline, "extract_metadata_and_thumb", lambda *_a, **_k: {
            "title": "T", "description": "", "genre": "", "date": "", "purl": "", "artist": "",
            "thumb_path": None, "recording_start": None,
        })

        def fake_split(work_path, output_dir=None, segment_time_sec=None):
            seen["segment_time_sec"] = segment_time_sec
            return [str(video)]

        monkeypatch.setattr(pipeline, "split_video_if_needed", fake_split)
        monkeypatch.setattr(pipeline, "upload_single_video", lambda **_k: "vid")
        pipeline.process_single_file(str(video), manage_files=False)
        return video, seen

    def test_fresh_file_uses_config(self, pipeline, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", 3 * 3600)
        _video, seen = self._run(pipeline, monkeypatch, tmp_path)
        assert seen["segment_time_sec"] == 3 * 3600

    def test_retry_uses_length_from_progress(self, pipeline, config, fileutils, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", 3 * 3600)
        fileutils.save_segment_progress(str(tmp_path / "video.mkv"), {"video_part00.mkv": "id1"}, 36000)
        _video, seen = self._run(pipeline, monkeypatch, tmp_path)
        assert seen["segment_time_sec"] == 36000

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "yt-upload.py"
SPEC = importlib.util.spec_from_file_location("yt_upload", MODULE_PATH)
yt_upload = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(yt_upload)


class UploaderRegressionTests(unittest.TestCase):
    def test_unique_path_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            existing = Path(temp_dir) / "video.mp4"
            existing.write_bytes(b"existing")

            result = yt_upload.unique_path(temp_dir, "video.mp4")

            self.assertEqual(result, str(Path(temp_dir) / "video_1.mp4"))
            self.assertEqual(existing.read_bytes(), b"existing")

    def test_split_includes_part_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source = work_dir / "recording.mkv"
            source.write_bytes(b"source")

            def fake_run(command, **kwargs):
                if command[0] == "ffprobe":
                    return subprocess.CompletedProcess(command, 0, stdout="72001\n", stderr="")
                (work_dir / "recording_part00.mkv").write_bytes(b"part 0")
                (work_dir / "recording_part01.mkv").write_bytes(b"part 1")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch.object(yt_upload, "WORK_DIR", str(work_dir)):
                with patch.object(yt_upload.subprocess, "run", side_effect=fake_run):
                    segments = yt_upload.split_video_if_needed(str(source))

            self.assertEqual(
                [Path(segment).name for segment in segments],
                ["recording_part00.mkv", "recording_part01.mkv"],
            )


if __name__ == "__main__":
    unittest.main()

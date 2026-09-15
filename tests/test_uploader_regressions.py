import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from yt_upload import config, fileutils, media  # noqa: E402


class UploaderRegressionTests(unittest.TestCase):
    def test_unique_path_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            existing = Path(temp_dir) / "video.mp4"
            existing.write_bytes(b"existing")

            result = fileutils.unique_path(temp_dir, "video.mp4")

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

            # WORK_DIR lebt in config.py; split_video_if_needed (media.py) liest es
            # zur Laufzeit als config.WORK_DIR, daher hier auf config patchen.
            # subprocess.run wird dagegen dort gepatcht, wo split_video_if_needed
            # es tatsächlich aufruft: media.py importiert subprocess selbst.
            with patch.object(config, "WORK_DIR", str(work_dir)):
                with patch.object(media.subprocess, "run", side_effect=fake_run):
                    segments = media.split_video_if_needed(str(source))

            self.assertEqual(
                [Path(segment).name for segment in segments],
                ["recording_part00.mkv", "recording_part01.mkv"],
            )


if __name__ == "__main__":
    unittest.main()

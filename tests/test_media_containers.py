"""
Integrationstests für media.py gegen echte, zur Laufzeit per ffmpeg erzeugte
Videodateien in allen unterstützten Containern (daemon.VIDEO_EXTENSIONS).

Bewusst keine Binärdateien im Repo: jedes Testvideo ist wenige Sekunden lang,
64x64 Pixel groß und wird einmal pro Testlauf in ein tmp-Verzeichnis gebaut.
Ohne ffmpeg/ffprobe im PATH werden die Tests übersprungen (CI installiert es).
"""

import contextlib
import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe nicht installiert",
)

TITLE = "Container Test Titel"
COMMENT = "Beschreibung aus dem Container"
DURATION_SEC = 4

# Cover bewusst mit anderer Auflösung als das Video (64x64), damit sich im
# Test unterscheiden lässt, ob das Thumbnail aus dem Cover oder aus einem
# gerenderten Frame stammt.
COVER_SIZE = (48, 32)

# Codecs pro Container, wie sie typischerweise in freier Wildbahn vorkommen
# (WebM erlaubt nur VP8/VP9/AV1 + Vorbis/Opus).
CONTAINERS = {
    ".mkv": ["-c:v", "libx264", "-c:a", "aac"],
    ".mp4": ["-c:v", "libx264", "-c:a", "aac"],
    ".m4v": ["-c:v", "libx264", "-c:a", "aac"],
    ".mov": ["-c:v", "libx264", "-c:a", "aac"],
    ".webm": ["-c:v", "libvpx-vp9", "-c:a", "libopus"],
}

# Keyframe jede Sekunde: Stream-Copy-Splitting kann nur an Keyframes
# schneiden, ohne -g läge bei einem 4s-Testvideo nur ein einziger vor.
KEYFRAME_ARGS = ["-g", "10"]

# Container, die ein eingebettetes Cover tragen können. WebM erlaubt weder
# Attachments noch attached_pic-Streams, und ffmpegs mov-Muxer verwirft
# attached_pic-Streams beim Schreiben (kein covr-Atom in .mov).
COVER_CONTAINERS = [".mkv", ".mp4", ".m4v"]


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _probe_size(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(res.stdout)["streams"][0]
    return stream["width"], stream["height"]


@pytest.fixture(scope="module")
def cover_jpg(tmp_path_factory):
    path = tmp_path_factory.mktemp("cover") / "cover.jpg"
    w, h = COVER_SIZE
    _ffmpeg("-f", "lavfi", "-i", f"color=c=red:s={w}x{h}", "-frames:v", "1", str(path))
    return path


def _make_video(path, codec_args, cover=None):
    inputs = [
        "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={DURATION_SEC}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION_SEC}",
    ]
    maps = ["-map", "0:v", "-map", "1:a"]
    cover_args = []
    if cover is not None and path.suffix == ".mkv":
        cover_args = ["-attach", str(cover), "-metadata:s:t", "mimetype=image/jpeg"]
    elif cover is not None:
        inputs += ["-i", str(cover)]
        maps += ["-map", "2:v"]
        cover_args = ["-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"]
    _ffmpeg(
        *inputs, *maps, *codec_args, *KEYFRAME_ARGS, *cover_args,
        "-metadata", f"title={TITLE}", "-metadata", f"comment={COMMENT}",
        str(path),
    )
    return path


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    """Ein Video ohne Cover pro Container."""
    base = tmp_path_factory.mktemp("videos")
    return {ext: _make_video(base / f"plain{ext}", args) for ext, args in CONTAINERS.items()}


@pytest.fixture(scope="module")
def videos_with_cover(tmp_path_factory, cover_jpg):
    base = tmp_path_factory.mktemp("covers")
    return {
        ext: _make_video(base / f"cover{ext}", CONTAINERS[ext], cover=cover_jpg)
        for ext in COVER_CONTAINERS
    }


@pytest.fixture
def cleanup_thumbs():
    """extract_metadata_and_thumb() legt das Thumbnail im System-Tempdir ab."""
    thumbs = []
    yield thumbs
    for t in thumbs:
        if t:
            with contextlib.suppress(OSError):
                os.remove(t)


def test_containers_match_daemon_extensions(daemon):
    assert set(CONTAINERS) == set(daemon.VIDEO_EXTENSIONS)


@pytest.mark.parametrize("ext", list(CONTAINERS))
class TestRealContainers:
    def test_file_is_ready_and_valid(self, media, videos, ext):
        assert media.is_file_ready_and_valid(str(videos[ext]), wait_interval=0, max_checks=2)

    def test_extracts_tags_and_duration(self, media, videos, ext, cleanup_thumbs):
        meta = media.extract_metadata_and_thumb(str(videos[ext]))
        cleanup_thumbs.append(meta["thumb_path"])
        assert meta["title"] == TITLE
        assert meta["description"] == COMMENT
        assert meta["duration"] in (DURATION_SEC - 1, DURATION_SEC)

    def test_falls_back_to_rendered_frame_without_cover(self, media, videos, ext, cleanup_thumbs):
        meta = media.extract_metadata_and_thumb(str(videos[ext]))
        cleanup_thumbs.append(meta["thumb_path"])
        assert meta["thumb_path"]
        assert _probe_size(meta["thumb_path"]) == (64, 64)

    def test_splits_into_segments_with_same_container(self, media, config, videos, ext, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", 1)
        work = tmp_path / f"long{ext}"
        shutil.copy(videos[ext], work)

        segments = media.split_video_if_needed(str(work), output_dir=str(tmp_path))

        assert len(segments) >= 2
        assert all(s.endswith(ext) for s in segments)
        for s in segments:
            assert _probe_size(s) == (64, 64)


@pytest.mark.parametrize("ext", COVER_CONTAINERS)
def test_uses_embedded_cover_as_thumbnail(media, videos_with_cover, ext, cleanup_thumbs):
    meta = media.extract_metadata_and_thumb(str(videos_with_cover[ext]))
    cleanup_thumbs.append(meta["thumb_path"])
    assert meta["thumb_path"]
    assert _probe_size(meta["thumb_path"]) == COVER_SIZE

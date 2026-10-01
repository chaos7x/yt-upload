"""
Tests für die Aufnahme-Startzeit: RECORDING_START-Tag (von tw-recorder)
-> recordingDate mit Uhrzeit und "Aufnahmestart: ..."-Zeile in der
Beschreibung, inklusive Zeitversatz pro Segment bei Splitting.
"""

import time
from argparse import Namespace
from datetime import UTC, datetime

import pytest

DEFAULT_META = {
    "title": "Titel",
    "description": "Beschreibung",
    "purl": None,
    "genre": None,
    "date": "20261001",
    "creation_time": "2020-01-01T12:00:00.000000Z",
    "recording_start": "2026-10-01T20:15:00Z",
    "artist": None,
    "thumb_path": None,
    "duration": 100,
}


@pytest.fixture
def utc_tz(monkeypatch):
    """Lokale Zeitzone für format_start_time_line() deterministisch auf UTC setzen."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestParseStartTime:
    def test_ffprobe_format(self, text_utils):
        assert text_utils.parse_start_time("2026-10-01T20:15:00.000000Z") == datetime(2026, 10, 1, 20, 15, tzinfo=UTC)

    def test_offset_is_converted_to_utc(self, text_utils):
        assert text_utils.parse_start_time("2026-10-01T22:15:00+02:00") == datetime(2026, 10, 1, 20, 15, tzinfo=UTC)

    def test_naive_value_is_assumed_utc(self, text_utils):
        assert text_utils.parse_start_time("2026-10-01 20:15:00") == datetime(2026, 10, 1, 20, 15, tzinfo=UTC)

    def test_midnight_is_a_valid_start(self, text_utils):
        assert text_utils.parse_start_time("2026-10-01T00:00:00Z") == datetime(2026, 10, 1, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, "", "kaputt"])
    def test_missing_or_invalid_returns_none(self, text_utils, value):
        assert text_utils.parse_start_time(value) is None


class TestFormatting:
    def test_recording_datetime(self, text_utils):
        start = datetime(2026, 10, 1, 20, 15, tzinfo=UTC)
        assert text_utils.format_recording_datetime(start) == "2026-10-01T20:15:00Z"

    def test_start_time_line_uses_local_tz(self, text_utils, utc_tz):
        start = datetime(2026, 10, 1, 20, 15, tzinfo=UTC)
        assert text_utils.format_start_time_line(start) == "Aufnahmestart: 01.10.2026 20:15 UTC"

    def test_segment_offset(self, text_utils, config, monkeypatch):
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", 3600)
        start = datetime(2026, 10, 1, 20, 15, tzinfo=UTC)
        assert text_utils.segment_start_time(start, 2) == datetime(2026, 10, 1, 22, 15, tzinfo=UTC)
        assert text_utils.segment_start_time(None, 2) is None


def _run(pipeline, monkeypatch, tmp_path, meta, segments=1, args=None):
    calls = []
    video = tmp_path / "video.mkv"
    video.write_bytes(b"fake video content")
    monkeypatch.setattr(pipeline, "is_file_ready_and_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(pipeline, "extract_metadata_and_thumb", lambda *_a, **_k: dict(meta))
    seg_paths = [str(video)] if segments == 1 else [str(tmp_path / f"seg{i}.mkv") for i in range(segments)]
    monkeypatch.setattr(pipeline, "split_video_if_needed", lambda work_path, output_dir=None: seg_paths)
    monkeypatch.setattr(pipeline, "upload_single_video", lambda **k: calls.append(k) or f"id{len(calls)}")
    pipeline.process_single_file(str(video), args=args, manage_files=False)
    return calls


class TestPipelineStartTime:
    def test_start_time_goes_to_recording_date_and_description(self, pipeline, monkeypatch, tmp_path, utc_tz):
        calls = _run(pipeline, monkeypatch, tmp_path, DEFAULT_META)
        assert calls[0]["rec_date"] == "2026-10-01T20:15:00Z"
        assert calls[0]["desc"].startswith("Aufnahmestart: 01.10.2026 20:15 UTC\n\nBeschreibung")

    def test_description_line_can_be_disabled(self, pipeline, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "ADD_START_TIME_TO_DESCRIPTION", False)
        calls = _run(pipeline, monkeypatch, tmp_path, DEFAULT_META)
        assert "Aufnahmestart" not in calls[0]["desc"]
        assert calls[0]["rec_date"] == "2026-10-01T20:15:00Z"

    def test_cli_recording_date_wins(self, pipeline, monkeypatch, tmp_path):
        calls = _run(pipeline, monkeypatch, tmp_path, DEFAULT_META, args=Namespace(recording_date="2020-01-01"))
        assert calls[0]["rec_date"] == "2020-01-01"

    def test_without_recording_start_falls_back_to_date_tag(self, pipeline, monkeypatch, tmp_path):
        """creation_time allein (z.B. aus einem yt-dlp-Download) wird bewusst ignoriert."""
        meta = dict(DEFAULT_META, recording_start=None)
        calls = _run(pipeline, monkeypatch, tmp_path, meta)
        assert calls[0]["rec_date"] == "20261001"
        assert "Aufnahmestart" not in calls[0]["desc"]

    def test_split_segments_get_offset_start_times(self, pipeline, config, monkeypatch, tmp_path, utc_tz):
        monkeypatch.setattr(config, "SEGMENT_TIME_SEC", 36000)
        calls = _run(pipeline, monkeypatch, tmp_path, DEFAULT_META, segments=2)
        assert [c["rec_date"] for c in calls] == ["2026-10-01T20:15:00Z", "2026-10-02T06:15:00Z"]
        assert calls[1]["desc"].startswith("Aufnahmestart: 02.10.2026 06:15 UTC")

"""
Tests für load_segment_progress/save_segment_progress/clear_segment_progress.

Diese Funktionen sind der Kern des Fixes für den Segment-Retry-Datenverlust-Bug:
Ohne funktionierende Persistenz würden bereits erfolgreich hochgeladene Segmente
bei einem Fehler im nächsten Segment erneut hochgeladen werden. Ein Test hier
hätte den ursprünglichen Bug beim Schreiben abgefangen.
"""

import json


class TestSegmentProgress:
    def test_no_progress_file_returns_empty_dict(self, yt_upload, tmp_path):
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        assert yt_upload.load_segment_progress(str(work_path)) == {}

    def test_save_then_load_roundtrip(self, yt_upload, tmp_path):
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        yt_upload.save_segment_progress(str(work_path), {"seg1.mkv": "abc123"})
        assert yt_upload.load_segment_progress(str(work_path)) == {"seg1.mkv": "abc123"}

    def test_progress_file_uses_dot_prefix_sidecar_name(self, yt_upload, tmp_path):
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        yt_upload.save_segment_progress(str(work_path), {"seg1.mkv": "abc123"})

        expected_sidecar = tmp_path / ".myvideo.mkv.progress.json"
        assert expected_sidecar.is_file()

    def test_clear_removes_progress_file(self, yt_upload, tmp_path):
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        yt_upload.save_segment_progress(str(work_path), {"seg1.mkv": "abc123"})
        yt_upload.clear_segment_progress(str(work_path))

        assert yt_upload.load_segment_progress(str(work_path)) == {}

    def test_clear_on_nonexistent_file_does_not_raise(self, yt_upload, tmp_path):
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        # Darf nicht crashen, auch wenn nie etwas gespeichert wurde
        yt_upload.clear_segment_progress(str(work_path))

    def test_corrupt_progress_file_falls_back_to_empty_dict(self, yt_upload, tmp_path):
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        sidecar = tmp_path / ".myvideo.mkv.progress.json"
        sidecar.write_text("{not valid json")

        # Darf nicht crashen - kaputte Progress-Datei bedeutet "ohne Fortschritt starten"
        assert yt_upload.load_segment_progress(str(work_path)) == {}

    def test_progress_survives_multiple_segments(self, yt_upload, tmp_path):
        """
        Simuliert den eigentlichen Bugfix-Fall: Segment 1 erfolgreich, Segment 2
        schlägt fehl. Der Fortschritt für Segment 1 darf dabei nicht verloren gehen.
        """
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        progress = yt_upload.load_segment_progress(str(work_path))
        progress["myvideo.mkv.part1.mkv"] = "video_id_1"
        yt_upload.save_segment_progress(str(work_path), progress)

        # Segment 2 schlägt fehl (kein zweiter save-Aufruf) - beim erneuten Laden
        # muss Segment 1 weiterhin als erledigt markiert sein.
        reloaded = yt_upload.load_segment_progress(str(work_path))
        assert reloaded == {"myvideo.mkv.part1.mkv": "video_id_1"}
        assert "myvideo.mkv.part2.mkv" not in reloaded

    def test_saved_content_is_valid_json(self, yt_upload, tmp_path):
        work_path = tmp_path / "myvideo.mkv"
        work_path.write_bytes(b"dummy")

        yt_upload.save_segment_progress(str(work_path), {"seg1.mkv": "abc123"})

        sidecar = tmp_path / ".myvideo.mkv.progress.json"
        with open(sidecar, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"seg1.mkv": "abc123"}

"""
Tests für process_single_file(manage_files=False): der manuelle CLI-Modus
(main.py, explizite Datei-Argumente) verarbeitet die übergebene Datei an
Ort und Stelle statt sie über WORK_DIR/DONE_DIR/CORRUPT_DIR/RETRY_DIR zu
verwalten - ein professioneller CLI-Nutzer erwartet nicht, dass seine Datei
verschoben wird, und braucht diese Verzeichnisse gar nicht erst angelegt.
"""

import os

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


def _patch_common(monkeypatch, pipeline, upload_side_effect=None):
    monkeypatch.setattr(pipeline, "is_file_ready_and_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(pipeline, "extract_metadata_and_thumb", lambda *_a, **_k: dict(DEFAULT_META))
    if upload_side_effect is None:
        monkeypatch.setattr(pipeline, "upload_single_video", lambda **_k: "VIDEO_ID_123")
    else:
        monkeypatch.setattr(pipeline, "upload_single_video", upload_side_effect)


class TestManageFilesFalseSuccess:
    def test_file_stays_in_place_no_split(self, pipeline, config, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake video content")

        _patch_common(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "split_video_if_needed", lambda work_path, output_dir=None: [work_path])

        pipeline.process_single_file(str(video), args=None, manage_files=False)

        assert video.is_file()
        assert video.read_bytes() == b"fake video content"
        # Keine WORK_DIR/DONE_DIR-Artefakte im selben Verzeichnis angelegt
        assert list(tmp_path.iterdir()) == [video]

    def test_split_segments_are_written_to_a_temp_dir_not_workdir(self, pipeline, config, tmp_path, monkeypatch):
        video = tmp_path / "video.mkv"
        video.write_bytes(b"fake long video")

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        monkeypatch.setattr(config, "WORK_DIR", str(work_dir))

        captured = {}

        def fake_split(work_path, output_dir=None):
            captured["output_dir"] = output_dir
            assert output_dir is not None
            assert output_dir != str(work_dir)
            seg0 = os.path.join(output_dir, "video_part00.mkv")
            seg1 = os.path.join(output_dir, "video_part01.mkv")
            open(seg0, "wb").close()
            open(seg1, "wb").close()
            return [seg0, seg1]

        _patch_common(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "split_video_if_needed", fake_split)

        pipeline.process_single_file(str(video), args=None, manage_files=False)

        # Original bleibt unangetastet liegen
        assert video.is_file()
        # Temp-Verzeichnis für die Segmente wurde nach Erfolg wieder aufgeräumt
        assert not os.path.isdir(captured["output_dir"])
        # WORK_DIR blieb komplett leer - keine Segmente landeten dort
        assert list(work_dir.iterdir()) == []

    def test_no_split_removes_unused_temp_dir(self, pipeline, config, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake video content")

        captured = {}

        def fake_split(work_path, output_dir=None):
            captured["output_dir"] = output_dir
            return [work_path]  # kein Splitting nötig

        _patch_common(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "split_video_if_needed", fake_split)

        pipeline.process_single_file(str(video), args=None, manage_files=False)

        # Das für den Splitting-Fall bereitgestellte Temp-Verzeichnis wurde
        # trotzdem übergeben (Signatur-Kompatibilität), aber wieder entfernt,
        # da es ungenutzt blieb.
        assert captured["output_dir"] is not None
        assert not os.path.isdir(captured["output_dir"])


class TestManageFilesFalseFailure:
    def test_upload_failure_leaves_file_untouched(self, pipeline, config, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake video content")

        corrupt_dir = tmp_path / "corrupt"
        retry_dir = tmp_path / "retry"
        corrupt_dir.mkdir()
        retry_dir.mkdir()
        monkeypatch.setattr(config, "CORRUPT_DIR", str(corrupt_dir))
        monkeypatch.setattr(config, "RETRY_DIR", str(retry_dir))

        def failing_upload(**_k):
            raise RuntimeError("simulierter Upload-Fehler")

        _patch_common(monkeypatch, pipeline, upload_side_effect=failing_upload)
        monkeypatch.setattr(pipeline, "split_video_if_needed", lambda work_path, output_dir=None: [work_path])

        pipeline.process_single_file(str(video), args=None, manage_files=False)

        assert video.is_file()
        assert list(corrupt_dir.iterdir()) == []
        assert list(retry_dir.iterdir()) == []

    def test_invalid_file_is_not_moved_to_corrupt(self, pipeline, config, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake video content")

        corrupt_dir = tmp_path / "corrupt"
        corrupt_dir.mkdir()
        monkeypatch.setattr(config, "CORRUPT_DIR", str(corrupt_dir))
        monkeypatch.setattr(pipeline, "is_file_ready_and_valid", lambda *_a, **_k: False)

        pipeline.process_single_file(str(video), args=None, manage_files=False)

        assert video.is_file()
        assert list(corrupt_dir.iterdir()) == []

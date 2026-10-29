"""
Tests für [force_private] / FORCE_PRIVATE_ARTISTS: Videos bestimmter Artists
(ARTIST-Tag) werden immer als 'private' hochgeladen, --privacy hat Vorrang.
"""

from argparse import Namespace

DEFAULT_META = {
    "title": "Titel",
    "description": "Beschreibung",
    "purl": "https://twitch.tv/streamer",
    "genre": None,
    "date": "20261029",
    "creation_time": None,
    "recording_start": None,
    "artist": "Streamer",
    "thumb_path": None,
    "duration": 100,
}


def _write_config(tmp_path, content):
    path = tmp_path / "upload.conf"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _load(config, tmp_path, monkeypatch, content, env=None):
    monkeypatch.setattr(config, "CONF_PATH", _write_config(tmp_path, content))
    monkeypatch.setattr(config, "CONF_D_DIR", "")
    monkeypatch.delenv("FORCE_PRIVATE_ARTISTS", raising=False)
    if env is not None:
        monkeypatch.setenv("FORCE_PRIVATE_ARTISTS", env)
    config.load_configuration()


def _run(pipeline, monkeypatch, tmp_path, meta, args=None):
    calls = []
    video = tmp_path / "video.mkv"
    video.write_bytes(b"fake video content")
    monkeypatch.setattr(pipeline, "is_file_ready_and_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(pipeline, "extract_metadata_and_thumb", lambda *_a, **_k: dict(meta))
    monkeypatch.setattr(pipeline, "split_video_if_needed", lambda work_path, output_dir=None: [str(video)])
    monkeypatch.setattr(pipeline, "upload_single_video", lambda **k: calls.append(k) or "id1")
    pipeline.process_single_file(str(video), args=args, manage_files=False)
    return calls


class TestForcePrivateConfig:
    def test_section_is_parsed_lowercase(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[force_private]\nStreamer = true\nanderer = false\n")
        assert config.FORCE_PRIVATE_ARTISTS == ["streamer"]

    def test_env_var_is_added(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[force_private]\nstreamer = true\n", env=" Zweiter , ,dritter")
        assert config.FORCE_PRIVATE_ARTISTS == ["dritter", "streamer", "zweiter"]

    def test_removed_entry_disappears_on_reload(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[force_private]\nstreamer = true\n")
        _write_config(tmp_path, "[settings]\nprivacy_status = public\n")
        config.load_configuration()
        assert config.FORCE_PRIVATE_ARTISTS == []


class TestForcePrivatePipeline:
    def test_matching_artist_is_uploaded_private(self, pipeline, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "VIDEO_PRIVACY", "public")
        monkeypatch.setattr(config, "FORCE_PRIVATE_ARTISTS", ["streamer"])
        calls = _run(pipeline, monkeypatch, tmp_path, DEFAULT_META)
        assert calls[0]["privacy"] == "private"

    def test_other_artist_keeps_config_default(self, pipeline, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "VIDEO_PRIVACY", "public")
        monkeypatch.setattr(config, "FORCE_PRIVATE_ARTISTS", ["streamer"])
        calls = _run(pipeline, monkeypatch, tmp_path, dict(DEFAULT_META, artist="jemand"))
        assert calls[0]["privacy"] == "public"

    def test_missing_artist_keeps_config_default(self, pipeline, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "VIDEO_PRIVACY", "unlisted")
        monkeypatch.setattr(config, "FORCE_PRIVATE_ARTISTS", ["streamer"])
        calls = _run(pipeline, monkeypatch, tmp_path, dict(DEFAULT_META, artist=None))
        assert calls[0]["privacy"] == "unlisted"

    def test_cli_privacy_wins(self, pipeline, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "FORCE_PRIVATE_ARTISTS", ["streamer"])
        calls = _run(pipeline, monkeypatch, tmp_path, DEFAULT_META, args=Namespace(privacy="public"))
        assert calls[0]["privacy"] == "public"

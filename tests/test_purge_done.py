"""
Tests für fileutils.purge_done() / --purge-done und done_retention_days /
DONE_RETENTION_DAYS. Die ctime lässt sich nicht per os.utime() zurückdatieren,
daher wird purge_done() ein künstliches "jetzt" übergeben.
"""

import os
import time

DAY = 86400


def _setup_done(config, monkeypatch, tmp_path, days=7):
    done_dir = tmp_path / "done"
    done_dir.mkdir()
    monkeypatch.setattr(config, "DONE_DIR", str(done_dir))
    monkeypatch.setattr(config, "DONE_RETENTION_DAYS", days)
    return done_dir


class TestPurgeDone:
    def test_old_file_is_deleted(self, fileutils, config, monkeypatch, tmp_path):
        done_dir = _setup_done(config, monkeypatch, tmp_path)
        (done_dir / "video.mkv").write_bytes(b"x")

        assert fileutils.purge_done(now=time.time() + 8 * DAY) == 1
        assert list(done_dir.iterdir()) == []

    def test_file_younger_than_retention_is_kept(self, fileutils, config, monkeypatch, tmp_path):
        done_dir = _setup_done(config, monkeypatch, tmp_path)
        (done_dir / "video.mkv").write_bytes(b"x")

        assert fileutils.purge_done(now=time.time() + 6 * DAY) == 0
        assert (done_dir / "video.mkv").exists()

    def test_old_mtime_alone_does_not_delete(self, fileutils, config, monkeypatch, tmp_path):
        # Verschieben nach DONE_DIR erhält die mtime (z.B. altes yt-dlp-Video),
        # gezählt wird ab Ankunft (ctime)
        done_dir = _setup_done(config, monkeypatch, tmp_path)
        video = done_dir / "video.mkv"
        video.write_bytes(b"x")
        old = time.time() - 365 * DAY
        os.utime(video, (old, old))

        assert fileutils.purge_done() == 0
        assert video.exists()

    def test_zero_disables_deletion(self, fileutils, config, monkeypatch, tmp_path):
        done_dir = _setup_done(config, monkeypatch, tmp_path, days=0)
        (done_dir / "video.mkv").write_bytes(b"x")

        assert fileutils.purge_done(now=time.time() + 1000 * DAY) == 0
        assert (done_dir / "video.mkv").exists()

    def test_missing_done_dir_returns_zero(self, fileutils, config, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "DONE_DIR", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(config, "DONE_RETENTION_DAYS", 7)

        assert fileutils.purge_done() == 0

    def test_skips_dotfiles_dirs_and_symlinks(self, fileutils, config, monkeypatch, tmp_path):
        done_dir = _setup_done(config, monkeypatch, tmp_path)
        (done_dir / ".hidden").write_bytes(b"x")
        (done_dir / "subdir").mkdir()
        (done_dir / "subdir" / "inner.mkv").write_bytes(b"x")
        outside = tmp_path / "outside.mkv"
        outside.write_bytes(b"x")
        (done_dir / "link.mkv").symlink_to(outside)

        assert fileutils.purge_done(now=time.time() + 30 * DAY) == 0
        assert (done_dir / ".hidden").exists()
        assert (done_dir / "subdir" / "inner.mkv").exists()
        assert (done_dir / "link.mkv").is_symlink()
        assert outside.exists()

    def test_other_pipeline_dirs_untouched(self, fileutils, config, monkeypatch, tmp_path):
        _setup_done(config, monkeypatch, tmp_path)
        corrupt = tmp_path / "corrupt"
        retry = tmp_path / "retry"
        for d in (corrupt, retry):
            d.mkdir()
            (d / "video.mkv").write_bytes(b"x")
        monkeypatch.setattr(config, "CORRUPT_DIR", str(corrupt))
        monkeypatch.setattr(config, "RETRY_DIR", str(retry))

        fileutils.purge_done(now=time.time() + 30 * DAY)

        assert (corrupt / "video.mkv").exists()
        assert (retry / "video.mkv").exists()


def _load(config, tmp_path, monkeypatch, content, env=None):
    path = tmp_path / "upload.conf"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "CONF_PATH", str(path))
    monkeypatch.setattr(config, "CONF_D_DIR", "")
    monkeypatch.delenv("DONE_RETENTION_DAYS", raising=False)
    if env is not None:
        monkeypatch.setenv("DONE_RETENTION_DAYS", env)
    config.load_configuration()


class TestDoneRetentionConfig:
    def test_default_is_seven(self, config, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DONE_RETENTION_DAYS", config._DEFAULTS["DONE_RETENTION_DAYS"])
        _load(config, tmp_path, monkeypatch, "[settings]\n")
        assert config.DONE_RETENTION_DAYS == 7

    def test_config_value(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[settings]\ndone_retention_days = 3\n")
        assert config.DONE_RETENTION_DAYS == 3

    def test_env_overrides_config(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[settings]\ndone_retention_days = 3\n", env="0")
        assert config.DONE_RETENTION_DAYS == 0

    def test_invalid_values_keep_previous(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[settings]\ndone_retention_days = 5\n")
        _load(config, tmp_path, monkeypatch, "[settings]\ndone_retention_days = viele\n", env="bald")
        assert config.DONE_RETENTION_DAYS == 5

    def test_negative_is_clamped_to_zero(self, config, tmp_path, monkeypatch):
        _load(config, tmp_path, monkeypatch, "[settings]\ndone_retention_days = -2\n")
        assert config.DONE_RETENTION_DAYS == 0


class TestParseArgumentsPurgeDone:
    def test_default_is_false(self, main_module):
        assert main_module.parse_arguments(["video.mp4"]).purge_done is False

    def test_flag_sets_true(self, main_module):
        assert main_module.parse_arguments(["--purge-done"]).purge_done is True

"""Tests für write_heartbeat() und run_healthcheck() (healthcheck.py).

HEALTH_FILE/HEALTH_STALE_SECONDS leben in config.py; healthcheck.py liest sie
zur Laufzeit als config.HEALTH_FILE - Patches müssen daher auf dem
config-Modul erfolgen.
"""

import os
import time


class TestWriteHeartbeatAndHealthcheck:
    def test_missing_heartbeat_file_is_unhealthy(self, healthcheck, config, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HEALTH_FILE", str(tmp_path / "does-not-exist.health"))
        assert healthcheck.run_healthcheck() == 1

    def test_fresh_heartbeat_is_healthy(self, healthcheck, config, tmp_path, monkeypatch):
        health_file = tmp_path / "yt-upload.health"
        monkeypatch.setattr(config, "HEALTH_FILE", str(health_file))
        monkeypatch.setattr(config, "HEALTH_STALE_SECONDS", 60)

        healthcheck.write_heartbeat()

        assert health_file.is_file()
        assert healthcheck.run_healthcheck() == 0

    def test_stale_heartbeat_is_unhealthy(self, healthcheck, config, tmp_path, monkeypatch):
        health_file = tmp_path / "yt-upload.health"
        monkeypatch.setattr(config, "HEALTH_FILE", str(health_file))
        monkeypatch.setattr(config, "HEALTH_STALE_SECONDS", 60)

        healthcheck.write_heartbeat()
        stale_time = time.time() - 120
        os.utime(health_file, (stale_time, stale_time))

        assert healthcheck.run_healthcheck() == 1

    def test_write_heartbeat_does_not_raise_if_unwritable(self, healthcheck, config, tmp_path, monkeypatch):
        """Ein Healthcheck-Problem darf den Upload nicht zum Absturz bringen."""
        monkeypatch.setattr(config, "HEALTH_FILE", str(tmp_path / "missing-dir" / "yt-upload.health"))

        healthcheck.write_heartbeat()  # darf nicht raisen

    def test_write_heartbeat_does_not_follow_symlink(self, healthcheck, config, tmp_path, monkeypatch):
        victim = tmp_path / "victim"
        victim.write_text("unverändert")
        health_file = tmp_path / "yt-upload.health"
        health_file.symlink_to(victim)
        monkeypatch.setattr(config, "HEALTH_FILE", str(health_file))

        healthcheck.write_heartbeat()

        assert victim.read_text() == "unverändert"

    def test_symlinked_heartbeat_is_unhealthy(self, healthcheck, config, tmp_path, monkeypatch):
        fresh = tmp_path / "fresh"
        fresh.write_text(str(time.time()))
        health_file = tmp_path / "yt-upload.health"
        health_file.symlink_to(fresh)
        monkeypatch.setattr(config, "HEALTH_FILE", str(health_file))

        assert healthcheck.run_healthcheck() == 1


class TestDefaultHealthDir:
    def test_uses_run_dir_if_present(self, config, monkeypatch):
        monkeypatch.setattr(config.os.path, "isdir", lambda p: p == "/var/run/yt-upload")
        assert config._default_health_dir() == "/var/run/yt-upload"

    def test_falls_back_to_tempdir(self, config, monkeypatch):
        monkeypatch.setattr(config.os.path, "isdir", lambda p: False)
        assert config._default_health_dir() == config.tempfile.gettempdir()

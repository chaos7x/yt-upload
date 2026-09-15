"""
Tests für is_syslog_daemon_running() (logging_setup.py) und _default_log_file()
(config.py - dort platziert, um einen Zirkelimport zu vermeiden, da config.py
_default_log_file() selbst für den LOG_FILE-Default braucht).

Beide Funktionen greifen auf absolute, systemweite Pfade zu (/proc, /log,
/var/log/yt-upload). Damit die Tests unabhängig davon laufen, was auf der
jeweiligen Test-Maschine tatsächlich installiert/gemountet ist, wird das
Filesystem-Verhalten per monkeypatch kontrolliert simuliert statt real
abgefragt.
"""

import os


class FakeProcCommFile:
    """Simuliert eine geöffnete /proc/<pid>/comm-Datei als Context Manager."""

    def __init__(self, content):
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._content


class TestIsSyslogDaemonRunning:
    def test_returns_false_without_proc_directory(self, logging_setup, monkeypatch):
        # Nicht-Linux-Systeme (kein /proc) sollen konservativ False liefern
        monkeypatch.setattr(logging_setup.os.path, "isdir", lambda path: False)
        assert logging_setup.is_syslog_daemon_running() is False

    def test_detects_running_rsyslogd(self, logging_setup, monkeypatch):
        monkeypatch.setattr(logging_setup.os.path, "isdir", lambda path: True)
        monkeypatch.setattr(logging_setup.os, "listdir", lambda path: ["111", "222", "not-a-pid"])

        comm_by_pid = {"111": "bash", "222": "rsyslogd"}

        def fake_open(path, mode="r", *args, **kwargs):
            for pid, name in comm_by_pid.items():
                if path == f"/proc/{pid}/comm":
                    return FakeProcCommFile(name)
            raise FileNotFoundError(path)

        monkeypatch.setattr("builtins.open", fake_open)

        assert logging_setup.is_syslog_daemon_running() is True

    def test_returns_false_when_no_matching_process(self, logging_setup, monkeypatch):
        monkeypatch.setattr(logging_setup.os.path, "isdir", lambda path: True)
        monkeypatch.setattr(logging_setup.os, "listdir", lambda path: ["111", "222"])

        comm_by_pid = {"111": "bash", "222": "python3"}

        def fake_open(path, mode="r", *args, **kwargs):
            for pid, name in comm_by_pid.items():
                if path == f"/proc/{pid}/comm":
                    return FakeProcCommFile(name)
            raise FileNotFoundError(path)

        monkeypatch.setattr("builtins.open", fake_open)

        assert logging_setup.is_syslog_daemon_running() is False

    def test_permission_denied_on_single_pid_is_skipped(self, logging_setup, monkeypatch):
        """Ein einzelner nicht lesbarer /proc/<pid>/comm darf die Suche nicht abbrechen."""
        monkeypatch.setattr(logging_setup.os.path, "isdir", lambda path: True)
        monkeypatch.setattr(logging_setup.os, "listdir", lambda path: ["111", "222"])

        def fake_open(path, mode="r", *args, **kwargs):
            if path == "/proc/111/comm":
                raise PermissionError(path)
            if path == "/proc/222/comm":
                return FakeProcCommFile("syslog-ng")
            raise FileNotFoundError(path)

        monkeypatch.setattr("builtins.open", fake_open)

        assert logging_setup.is_syslog_daemon_running() is True


class TestDefaultLogFile:
    def test_prefers_log_dir_if_it_exists(self, config, monkeypatch):
        real_exists = config.os.path.exists
        monkeypatch.setattr(
            config.os.path, "exists",
            lambda p: True if p == "/log" else real_exists(p)
        )
        assert config._default_log_file() == "/log/upload.log"

    def test_falls_back_to_var_log_if_writable(self, config, monkeypatch):
        real_exists = config.os.path.exists
        monkeypatch.setattr(
            config.os.path, "exists",
            lambda p: False if p == "/log" else real_exists(p)
        )
        monkeypatch.setattr(config.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr(config.os, "access", lambda *a, **k: True)

        expected = os.path.join("/var/log/yt-upload", "yt-upload.log")
        assert config._default_log_file() == expected

    def test_final_fallback_to_base_dir_if_var_log_unwritable(self, config, monkeypatch):
        real_exists = config.os.path.exists
        monkeypatch.setattr(
            config.os.path, "exists",
            lambda p: False if p == "/log" else real_exists(p)
        )

        def raise_oserror(*a, **k):
            raise OSError("Permission denied")

        monkeypatch.setattr(config.os, "makedirs", raise_oserror)

        expected = os.path.join(config.BASE_DIR, "upload.log")
        assert config._default_log_file() == expected

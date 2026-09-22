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


class TestIsDedicatedMount:
    """
    Testet _is_dedicated_mount() (config.py) - unterscheidet ein echtes
    Docker-Volume/Bind-Mount von einem gewöhnlichen, per `mkdir -p` fest ins
    Image gebackenen Verzeichnis (z.B. /log, /videos), das ohne diese
    Unterscheidung fälschlich als "gemountet" durchgehen würde.
    """

    def test_returns_false_if_path_does_not_exist(self, config, monkeypatch):
        monkeypatch.setattr(config.os.path, "isdir", lambda p: False)
        assert config._is_dedicated_mount("/log") is False

    def test_returns_false_for_plain_baked_in_directory_same_device(self, config, monkeypatch):
        """Gleiche st_dev wie das Elternverzeichnis = kein echter Mount, nur ein normaler Ordner."""
        monkeypatch.setattr(config.os.path, "isdir", lambda p: True)

        class FakeStat:
            def __init__(self, st_dev):
                self.st_dev = st_dev

        monkeypatch.setattr(config.os, "stat", lambda p: FakeStat(st_dev=1))

        assert config._is_dedicated_mount("/log") is False

    def test_returns_true_for_real_mount_different_device(self, config, monkeypatch):
        """Unterschiedliche st_dev zum Elternverzeichnis = tatsächlich eingehängtes Volume/Bind-Mount."""
        monkeypatch.setattr(config.os.path, "isdir", lambda p: True)

        class FakeStat:
            def __init__(self, st_dev):
                self.st_dev = st_dev

        def fake_stat(p):
            return FakeStat(st_dev=2) if p == "/log" else FakeStat(st_dev=1)

        monkeypatch.setattr(config.os, "stat", fake_stat)

        assert config._is_dedicated_mount("/log") is True

    def test_permission_error_on_stat_returns_false(self, config, monkeypatch):
        monkeypatch.setattr(config.os.path, "isdir", lambda p: True)

        def raise_oserror(p):
            raise OSError("Permission denied")

        monkeypatch.setattr(config.os, "stat", raise_oserror)

        assert config._is_dedicated_mount("/log") is False


class TestDefaultLogFile:
    def test_prefers_log_dir_if_dedicated_mount(self, config, monkeypatch):
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: p == "/log")
        assert config._default_log_file() == "/log/upload.log"

    def test_falls_back_to_var_log_if_writable(self, config, monkeypatch):
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: False)
        monkeypatch.setattr(config.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr(config.os, "access", lambda *a, **k: True)

        expected = os.path.join("/var/log/yt-upload", "yt-upload.log")
        assert config._default_log_file() == expected

    def test_final_fallback_to_base_dir_if_var_log_unwritable(self, config, monkeypatch):
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: False)

        def raise_oserror(*a, **k):
            raise OSError("Permission denied")

        monkeypatch.setattr(config.os, "makedirs", raise_oserror)

        expected = os.path.join(config.BASE_DIR, "upload.log")
        assert config._default_log_file() == expected


class TestSetupLoggingFileTrigger:
    """
    Deckt den eigentlich gemeldeten Bug ab: ohne echtes /log-Volume (nur das
    vom Dockerfile fest angelegte Verzeichnis) darf setup_logging() KEINEN
    RotatingFileHandler mehr aktivieren.
    """

    def _capture_handlers(self, logging_setup, monkeypatch):
        captured = {}
        monkeypatch.setattr(logging_setup.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))
        return captured

    def test_plain_baked_in_log_dir_without_real_mount_stays_stdout_only(self, logging_setup, config, monkeypatch):
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: False)
        monkeypatch.setattr(config, "LOG_FILE_EXPLICIT", False)
        monkeypatch.setattr(logging_setup, "is_syslog_daemon_running", lambda: False)
        captured = self._capture_handlers(logging_setup, monkeypatch)

        logging_setup.setup_logging()

        assert len(captured["handlers"]) == 1
        assert isinstance(captured["handlers"][0], logging_setup.logging.StreamHandler)

    def test_real_log_volume_mount_adds_rotating_file_handler(self, logging_setup, config, monkeypatch, tmp_path):
        """Docker-Volume: niemand sonst rotiert die Datei -> eigene RotatingFileHandler-Rotation noetig."""
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: True)
        monkeypatch.setattr(config, "LOG_FILE_EXPLICIT", False)
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "upload.log"))
        monkeypatch.setattr(logging_setup, "is_syslog_daemon_running", lambda: False)
        captured = self._capture_handlers(logging_setup, monkeypatch)

        logging_setup.setup_logging()

        assert len(captured["handlers"]) == 2
        assert isinstance(captured["handlers"][1], logging_setup.RotatingFileHandler)

    def test_explicit_log_file_config_adds_rotating_file_handler_even_without_mount(self, logging_setup, config, monkeypatch, tmp_path):
        """Frei gewaehlter Pfad: ebenfalls niemand sonst rotiert -> eigene Rotation."""
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: False)
        monkeypatch.setattr(config, "LOG_FILE_EXPLICIT", True)
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "upload.log"))
        monkeypatch.setattr(logging_setup, "is_syslog_daemon_running", lambda: False)
        captured = self._capture_handlers(logging_setup, monkeypatch)

        logging_setup.setup_logging()

        assert len(captured["handlers"]) == 2
        assert isinstance(captured["handlers"][1], logging_setup.RotatingFileHandler)

    def test_syslog_daemon_detected_adds_plain_file_handler_without_own_rotation(self, logging_setup, config, monkeypatch, tmp_path):
        """
        Ein laufender Syslog-Daemon impliziert praktisch immer auch logrotate
        (siehe logrotate.d/yt-upload) - die App soll dort NICHT zusaetzlich
        selbst per RotatingFileHandler rotieren, sonst kommen sich beide
        Mechanismen in die Quere.
        """
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: False)
        monkeypatch.setattr(config, "LOG_FILE_EXPLICIT", False)
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "upload.log"))
        monkeypatch.setattr(logging_setup, "is_syslog_daemon_running", lambda: True)
        captured = self._capture_handlers(logging_setup, monkeypatch)

        logging_setup.setup_logging()

        assert len(captured["handlers"]) == 2
        file_handler = captured["handlers"][1]
        assert isinstance(file_handler, logging_setup.logging.FileHandler)
        assert not isinstance(file_handler, logging_setup.RotatingFileHandler)

    def test_file_handler_creation_failure_falls_back_to_stdout_only(self, logging_setup, config, monkeypatch, tmp_path, caplog):
        """Weder RotatingFileHandler noch FileHandler duerfen bei einem Schreibfehler den Prozess abschiessen."""
        monkeypatch.setattr(config, "_is_dedicated_mount", lambda p: False)
        monkeypatch.setattr(config, "LOG_FILE_EXPLICIT", True)
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "unwritable-dir" / "upload.log"))
        monkeypatch.setattr(logging_setup, "is_syslog_daemon_running", lambda: False)

        def raise_permission_error(*a, **k):
            raise PermissionError("Permission denied")

        monkeypatch.setattr(logging_setup, "RotatingFileHandler", raise_permission_error)
        captured = self._capture_handlers(logging_setup, monkeypatch)

        with caplog.at_level("WARNING"):
            logging_setup.setup_logging()

        assert len(captured["handlers"]) == 1
        assert isinstance(captured["handlers"][0], logging_setup.logging.StreamHandler)
        assert any("nicht beschreibbar" in r.message for r in caplog.records)

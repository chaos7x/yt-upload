"""Logging-Setup: Syslog-Erkennung und Handler-Konfiguration."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from yt_upload import config

logger = logging.getLogger(__name__)


def is_syslog_daemon_running():
    """
    Prüft, ob ein klassischer Syslog-Daemon (rsyslog, syslog-ng, syslogd) aktiv läuft,
    indem /proc nach dem Prozessnamen durchsucht wird. Rein stdlib, kein subprocess/psutil.
    Auf Nicht-Linux-Systemen (kein /proc) liefert die Funktion konservativ False.
    """
    if not os.path.isdir("/proc"):
        return False
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm", "r") as f:
                name = f.read().strip()
            if name in ("rsyslogd", "syslog-ng", "syslogd"):
                return True
        except (OSError, PermissionError):
            continue
    return False


def setup_logging():
    """
    Initialisiert das Root-Logging:
    - stdout-Handler: immer aktiv, wird von journald/docker logs erfasst.
    - RotatingFileHandler: zusätzlich, ausgelöst durch (a) explizite LOG_FILE-
      Konfiguration (Config oder ENV), (b) ein tatsächlich als Docker-Volume
      gemountetes /log-Verzeichnis (siehe config._is_dedicated_mount() - eine
      reine Existenzprüfung reicht nicht, da das Dockerfile /log auch ganz
      ohne Mount fest ins Image anlegt), oder (c) einen tatsächlich laufenden
      klassischen Syslog-Daemon (rsyslog, syslog-ng, syslogd) auf
      Bare-Metal-/systemd-Systemen. Ohne einen dieser Gründe ist die eigene
      Logdatei nur eine unnötige zweite Datenhaltung neben dem Journal.
    """
    log_handlers = [logging.StreamHandler(sys.stdout)]
    docker_log_volume_mounted = config._is_dedicated_mount("/log")
    syslog_detected = is_syslog_daemon_running()
    file_log_error = None

    if config.LOG_FILE_EXPLICIT:
        trigger_reason = "explizite LOG_FILE-Konfiguration"
    elif docker_log_volume_mounted:
        trigger_reason = "/log als Docker-Volume gemountet"
    elif syslog_detected:
        trigger_reason = "Syslog-Daemon erkannt"
    else:
        trigger_reason = None

    if trigger_reason is not None:
        try:
            log_dir = os.path.dirname(config.LOG_FILE) or '.'
            os.makedirs(log_dir, exist_ok=True)
            log_handlers.append(
                RotatingFileHandler(config.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            )
        except OSError as e:
            file_log_error = str(e)

    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG_MODE else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=log_handlers
    )

    # HTTP-Bibliotheks-Rauschen (requests nutzt intern urllib3) unabhängig
    # vom eigenen Log-Level auf WARNING drosseln, damit z.B. "Resetting
    # dropped connection" o.ä. das eigentliche Debug-Logging nicht zumüllt.
    for noisy_logger_name in ("urllib3", "urllib3.connectionpool", "requests", "httpx"):
        logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)

    if trigger_reason is not None and file_log_error:
        logger.warning(f"Logdatei {config.LOG_FILE} nicht beschreibbar, verwende nur stdout: {file_log_error}")
    elif trigger_reason is not None:
        logger.info(f"Datei-Logging nach {config.LOG_FILE} aktiv ({trigger_reason}).")
    else:
        logger.info("Kein Syslog-Daemon/-Log-Verzeichnis/-Config erkannt - Logging nur nach stdout (journald/docker logs).")

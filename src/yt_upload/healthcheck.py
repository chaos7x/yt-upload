"""Heartbeat-Datei und externer Healthcheck (z.B. Docker HEALTHCHECK)."""

import logging
import os
import stat
import time

from yt_upload import config

logger = logging.getLogger(__name__)


def write_heartbeat():
    """
    Aktualisiert die Heartbeat-Datei mit dem aktuellen Zeitstempel. Wird an
    Stellen aufgerufen, die auch während einer langen Einzeloperation (z.B.
    mehrstündiger Resumable-Upload) regelmäßig durchlaufen werden - nicht nur
    nach Abschluss einer ganzen Datei -, damit ein --healthcheck echte Hänger
    erkennt statt nur "Prozess lebt noch".
    Schlägt der Schreibvorgang fehl (z.B. kein Schreibzugriff auf /tmp), wird
    das nur auf DEBUG geloggt - ein Healthcheck-Problem soll nicht den
    eigentlichen Upload zum Absturz bringen.
    """
    try:
        # O_NOFOLLOW: liegt die Datei im gemeinsamen /tmp (Fallback ohne
        # /var/run/yt-upload, siehe config._default_health_dir()), folgt der
        # Schreibzugriff keinem von einem anderen User untergeschobenen Symlink.
        fd = os.open(config.HEALTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        logger.debug(f"Konnte Heartbeat-Datei {config.HEALTH_FILE} nicht schreiben: {e}")


def run_healthcheck() -> int:
    """
    Leichtgewichtige Prüfung für externe Healthchecks (z.B. Docker HEALTHCHECK
    oder Kubernetes livenessProbe): prüft nur, ob die Heartbeat-Datei existiert
    und nicht älter als config.HEALTH_STALE_SECONDS ist. Lädt bewusst keine Config und
    legt keine Verzeichnisse an, damit der Aufruf schnell ist und keine
    Nebenwirkungen hat (wird typischerweise alle paar Sekunden aufgerufen).
    Gibt 0 (healthy) oder 1 (unhealthy) zurück, analog zu Exit-Codes.
    """
    try:
        st = os.lstat(config.HEALTH_FILE)
    except FileNotFoundError:
        print(f"UNHEALTHY: Heartbeat-Datei {config.HEALTH_FILE} nicht gefunden (Dämon noch nicht gestartet?).")
        return 1
    except OSError as e:
        print(f"UNHEALTHY: Heartbeat-Datei {config.HEALTH_FILE} nicht lesbar: {e}")
        return 1

    # lstat statt stat: ein Symlink (z.B. von einem anderen User im gemeinsamen
    # /tmp untergeschoben) zählt nicht als gültiger Heartbeat.
    if not stat.S_ISREG(st.st_mode):
        print(f"UNHEALTHY: Heartbeat-Datei {config.HEALTH_FILE} ist keine reguläre Datei.")
        return 1

    age = time.time() - st.st_mtime

    if age > config.HEALTH_STALE_SECONDS:
        print(f"UNHEALTHY: Heartbeat ist {age:.0f}s alt (Limit: {config.HEALTH_STALE_SECONDS}s).")
        return 1

    print(f"HEALTHY: Heartbeat ist {age:.0f}s alt.")
    return 0

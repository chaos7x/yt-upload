"""Heartbeat-Datei und externer Healthcheck (z.B. Docker HEALTHCHECK)."""

import logging
import os
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
        with open(config.HEALTH_FILE, "w") as f:
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
    if not os.path.isfile(config.HEALTH_FILE):
        print(f"UNHEALTHY: Heartbeat-Datei {config.HEALTH_FILE} nicht gefunden (Dämon noch nicht gestartet?).")
        return 1

    try:
        age = time.time() - os.path.getmtime(config.HEALTH_FILE)
    except OSError as e:
        print(f"UNHEALTHY: Heartbeat-Datei {config.HEALTH_FILE} nicht lesbar: {e}")
        return 1

    if age > config.HEALTH_STALE_SECONDS:
        print(f"UNHEALTHY: Heartbeat ist {age:.0f}s alt (Limit: {config.HEALTH_STALE_SECONDS}s).")
        return 1

    print(f"HEALTHY: Heartbeat ist {age:.0f}s alt.")
    return 0

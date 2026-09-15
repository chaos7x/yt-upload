"""
Dämon-Modus: Inotify-/Polling-Überwachung des Eingangsverzeichnisses,
Instanz-Lock und periodische Housekeeping-Aufgaben.
"""

import logging
import os
import tempfile
import time

from yt_upload import config
from yt_upload.healthcheck import write_heartbeat
from yt_upload.pipeline import process_single_file

logger = logging.getLogger(__name__)

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    # fcntl ist nur auf Unix verfügbar; auf anderen Plattformen läuft das Skript ohne Lockfile-Schutz weiter.
    HAS_FCNTL = False

# ------------------------------------------------------------------------------
# Inotify Import-Prüfung (Echtzeit-Dateisystemüberwachung unter Linux)
# Versucht das pyinotify bzw. inotify Modul zu laden, um Polling zu vermeiden.
# ------------------------------------------------------------------------------
try:
    import inotify.adapters
    import inotify.constants
    HAS_INOTIFY = True
except ImportError:
    inotify = None
    HAS_INOTIFY = False

# Watch-Mask beschränkt auf die tatsächlich relevanten Events (abgeschlossene
# Schreibvorgänge und Verschiebungen ins Verzeichnis). Ohne diese Einschränkung
# abonniert InotifyTree ALLE Event-Typen, inkl. reiner Verzeichnis-Lesezugriffe
# (IN_OPEN/IN_ACCESS/IN_CLOSE_NOWRITE mit IN_ISDIR) - genau solche Zugriffe
# erzeugt aber find_existing_video() selbst via os.walk() auf dem überwachten
# Baum. Ohne Filterung entsteht dadurch eine sich selbst befeuernde
# Event-Schleife (eigener Scan -> eigene Events -> erneuter Trigger -> ...),
# die unabhängig von echten Video-Uploads dauerhaft CPU verbraucht.
WATCH_MASK = (
    (inotify.constants.IN_CLOSE_WRITE | inotify.constants.IN_MOVED_TO)
    if HAS_INOTIFY else None
)


# ==============================================================================
# INOTIFY & EINGANGSÜBERWACHUNG
# ==============================================================================
def find_existing_video(target_dir=None):
    """Sucht rekursiv nach kompatiblen Videodateien im Eingangsverzeichnis."""
    if target_dir is None:
        target_dir = config.IN_DIR
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")
    if not os.path.isdir(target_dir):
        return None
    for root, _, files in os.walk(target_dir):
        for file in files:
            if file.lower().endswith(valid_exts):
                return os.path.join(root, file)
    return None


_last_periodic_tasks_run = 0.0


def run_periodic_tasks(min_interval_sec=10):
    """
    Führt config.load_configuration()+write_heartbeat() gedrosselt aus (max. alle
    min_interval_sec Sekunden), statt bei jedem einzelnen inotify-Event.
    Ohne diese Drosselung kann eine Event-Flut (z.B. während eine große Datei
    gerade per rsync/scp nach config.IN_DIR kopiert wird - viele IN_MODIFY-Events pro
    Sekunde) die CPU durch permanentes Config-Neuladen auslasten, obwohl der
    Daemon aus Nutzersicht "idle" ist (noch kein Upload gestartet).
    """
    global _last_periodic_tasks_run
    now = time.monotonic()
    if now - _last_periodic_tasks_run < min_interval_sec:
        return
    _last_periodic_tasks_run = now
    config.load_configuration(log_changes=True)
    write_heartbeat()


def wait_for_input(target_dir=None, inotify_adapter=None):
    if target_dir is None:
        target_dir = config.IN_DIR
    """
    Blockiert den Prozess im Dämonenmodus, bis eine neue Datei per inotify signalisiert
    oder per Polling-Fallback im Eingangsordner gefunden wird.
    """
    config.load_configuration(log_changes=True)

    # Prüfe zuerst, ob bereits verarbeitbare Dateien im Ordner liegen
    existing = find_existing_video(target_dir)
    if existing:
        logger.info(f"Bestehende Datei gefunden: {existing}")
        return existing

    logger.info(f"Warte via inotify auf neue Dateien in {target_dir}...")
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")

    # Fallback-Schleife falls inotify nicht im System geladen ist
    if not HAS_INOTIFY or inotify_adapter is None:
        if not HAS_INOTIFY:
            logger.warning("inotify-Modul nicht verfügbar, nutze Polling-Fallback.")
        while True:
            time.sleep(10)
            config.load_configuration(log_changes=True)
            write_heartbeat()
            existing = find_existing_video(target_dir)
            if existing:
                return existing

    # Event-Loop für inotify
    while True:
        try:
            # yield_nones=True sorgt dafür, dass die Schleife auch ohne
            # Dateisystem-Events alle timeout_s Sekunden die Kontrolle
            # zurückbekommt (Config-Reload, Heartbeat, Polling-Fallback).
            # Mit yield_nones=False (wie zuvor) wurde der "if event is None"-
            # Zweig unten nie erreicht - totes Coder, das effektiv jede
            # periodische Prüfung während des Wartens verhinderte.
            for event in inotify_adapter.event_gen(yield_nones=True, timeout_s=10):
                run_periodic_tasks()

                if event is None:
                    existing = find_existing_video(target_dir)
                    if existing:
                        logger.info(f"Datei via Fallback-Timer erkannt: {existing}")
                        return existing
                    continue

                (_, type_names, path, filename) = event

                # Zusätzliche Absicherung (falls die installierte inotify-Version
                # mask= ignoriert): reine Verzeichnis-Events sofort verwerfen,
                # bevor unnötige Vergleiche/Logs anfallen.
                if "IN_ISDIR" in type_names:
                    continue

                # Reagiere auf abgeschlossene Schreibvorgänge oder Verschiebungen
                if any(t in type_names for t in ["IN_CLOSE_WRITE", "IN_MOVED_TO"]) and filename.lower().endswith(valid_exts):
                    full_path = os.path.join(path, filename)
                    if os.path.isfile(full_path):
                        logger.info(f"Datei erfolgreich via inotify erkannt: {full_path}")
                        return full_path
        except Exception as e:  # noqa: BLE001 - Sicherheitsnetz für den gesamten Inotify-Loop; die inotify-Bibliothek kann diverse, nicht klar typisierte Fehler werfen
            logger.error(f"Fehler beim Inotify-Observer: {e}")
            time.sleep(5)
            existing = find_existing_video(target_dir)
            if existing:
                return existing


_lock_file_handle = None


def acquire_instance_lock():
    """
    Verhindert per exklusivem Filesystem-Lock, dass zwei Instanzen des Skripts
    (z.B. Daemon + Auto-Modus, oder zwei Daemons) gleichzeitig dieselben
    IN_DIR/WORK_DIR-Verzeichnisse bearbeiten und sich Dateien gegenseitig wegschnappen.
    Gibt True zurück, wenn der Lock erfolgreich erworben wurde.
    """
    global _lock_file_handle

    if not HAS_FCNTL:
        logger.warning("fcntl nicht verfügbar (kein Unix-System) - Lockfile-Schutz übersprungen.")
        return True

    lock_path = os.path.join(tempfile.gettempdir(), "yt-upload.lock")
    try:
        # Handle bleibt bewusst für die gesamte Prozesslaufzeit offen, damit der
        # flock() gehalten wird; ein `with`-Block würde ihn sofort wieder
        # schließen und den Lock damit freigeben.
        _lock_file_handle = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file_handle.write(str(os.getpid()))
        _lock_file_handle.flush()
        return True
    except (OSError, BlockingIOError):
        from yt_upload import __title__
        logger.error(
            f"Es läuft bereits eine andere Instanz von {__title__} (Lock: {lock_path}). Breche ab."
        )
        return False


def run_daemon():
    """
    Dauerhafte Überwachung des Eingangsverzeichnisses mittels Inotify (mit
    Polling-Fallback). Läuft bis KeyboardInterrupt (Ctrl+C / SIGINT).
    """
    logger.info("Starte Dämon-Modus...")

    current_watched_dir = None
    inotify_adapter = None

    try:
        while True:
            config.load_configuration(log_changes=True)
            write_heartbeat()

            # Bei Pfadänderung inotify Tree neu initialisieren
            if HAS_INOTIFY and current_watched_dir != config.IN_DIR:
                try:
                    logger.info(f"Initialisiere InotifyTree auf: {config.IN_DIR}")
                    inotify_adapter = inotify.adapters.InotifyTree(config.IN_DIR, mask=WATCH_MASK)
                    current_watched_dir = config.IN_DIR
                except Exception as e:  # noqa: BLE001 - inotify-Bibliothek hat keine eng gefasste Exception-Hierarchie; jeder Fehler hier soll auf den Polling-Fallback zurückfallen statt den Daemon abzubrechen
                    logger.error(f"Konnte InotifyTree für {config.IN_DIR} nicht initialisieren: {e}")
                    inotify_adapter = None

            input_file = wait_for_input(config.IN_DIR, inotify_adapter=inotify_adapter)
            if input_file:
                process_single_file(input_file, args=None)
    except KeyboardInterrupt:
        logger.info("Dämon-Modus beendet.")

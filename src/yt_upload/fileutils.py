"""
Dateisystem-Helfer: Zielverzeichnisse, eindeutige Pfade, Segment-Fortschritts-
Persistenz und Aufräum-Routinen.

Bewusst getrennt von pipeline.py (statt process_single_file dort mit
reinzupacken), weil sowohl pipeline.py als auch youtube_api.py
(cleanup_generated_thumbnail) diese Funktionen brauchen - ein direktes
pipeline<->youtube_api-Import wäre sonst zirkulär.
"""

import json
import logging
import os
import sys
import tempfile

from yt_upload import config

logger = logging.getLogger(__name__)


def ensure_directories():
    """Stellt sicher, dass alle notwendigen Zielverzeichnisse auf dem Dateisystem existieren."""
    for d in [config.IN_DIR, config.WORK_DIR, config.DONE_DIR, config.CORRUPT_DIR, config.RETRY_DIR]:
        try:
            os.makedirs(d, exist_ok=True)
        except (PermissionError, OSError) as e:
            sys.stderr.write(f"Warnung: Kann Verzeichnis {d} nicht anlegen ({e}).\n")


def unique_path(directory, filename):
    """
    Generiert einen eindeutigen Dateipfad durch Anhängen von Zählern, falls die Datei existiert.
    Hinweis: Prüfung und späteres Verschieben sind nicht atomar (TOCTOU); das ist unkritisch,
    solange acquire_instance_lock() parallele Instanzen desselben Skripts verhindert.
    """
    candidate = os.path.join(directory, filename)
    if not os.path.lexists(candidate):
        return candidate

    stem, extension = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = os.path.join(directory, f"{stem}_{counter}{extension}")
        if not os.path.lexists(candidate):
            return candidate
        counter += 1


def resolve_target_path(directory, filename):
    """Bestimmt das Ziel basierend auf der config.ALLOW_OVERWRITE Einstellung."""
    candidate = os.path.join(directory, filename)
    if config.ALLOW_OVERWRITE:
        return candidate
    return unique_path(directory, filename)


def _progress_file_path(work_path):
    """Pfad der Sidecar-JSON-Datei, die bereits erfolgreich hochgeladene Segmente eines Jobs protokolliert."""
    directory = os.path.dirname(work_path)
    filename = os.path.basename(work_path)
    return os.path.join(directory, f".{filename}.progress.json")


def load_segment_progress(work_path):
    """Lädt {segment_dateiname: video_id} bereits erfolgreich hochgeladener Segmente, falls vorhanden."""
    progress_path = _progress_file_path(work_path)
    if not os.path.isfile(progress_path):
        return {}
    try:
        with open(progress_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Konnte Progress-Datei {progress_path} nicht lesen, starte ohne Fortschritt: {e}")
        return {}


def save_segment_progress(work_path, progress):
    """Persistiert den aktuellen Fortschritt sofort nach jedem erfolgreichen Segment-Upload."""
    progress_path = _progress_file_path(work_path)
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(progress, f)
    except OSError as e:
        logger.warning(f"Konnte Progress-Datei {progress_path} nicht schreiben: {e}")


def clear_segment_progress(work_path):
    """Entfernt die Progress-Datei nach vollständigem Erfolg (oder wenn kein Segment fertig wurde)."""
    progress_path = _progress_file_path(work_path)
    try:
        if os.path.isfile(progress_path):
            os.unlink(progress_path)
    except OSError as e:
        logger.warning(f"Konnte Progress-Datei {progress_path} nicht löschen: {e}")


def cleanup_work_files(work_paths, is_error=False):
    """Löscht temporär erzeugte Arbeits- oder Segmentdateien aus dem WORK-Ordner."""
    log_func = logger.warning if is_error else logger.info
    log_func("Bereinige Dateien des aktuellen Jobs im WORK-Verzeichnis...")
    for item_path in work_paths:
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.unlink(item_path)
        except OSError as e:
            logger.error(f"Fehler beim Löschen von {item_path}: {e}")


def cleanup_generated_thumbnail(thumb_path):
    """Löscht automatisch erzeugte Thumbnails aus dem temporären Verzeichnis."""
    if not thumb_path:
        return

    temp_dir = os.path.abspath(tempfile.gettempdir())
    candidate = os.path.abspath(thumb_path)
    # Nur Dateien löschen, die im Temp-Ordner liegen und das Präfix yt_thumb_ tragen
    if os.path.dirname(candidate) != temp_dir or not os.path.basename(candidate).startswith("yt_thumb_"):
        return

    try:
        if os.path.isfile(candidate):
            os.unlink(candidate)
    except OSError as e:
        logger.warning(f"Generiertes Thumbnail kann nicht gelöscht werden ({candidate}): {e}")

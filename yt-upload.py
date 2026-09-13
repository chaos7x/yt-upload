#!/usr/bin/env python3
# Copyright (C) 2026 Chaos7x
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

# ==============================================================================
# YouTube Video Uploader & Auto-Archiver
# ==============================================================================

"""
Automatischer YouTube Video Uploader & CLI-Uploader (YouTube Data API v3)
Bietet drei Betriebsmodi:
1. Manuell (CLI): Upload einzelner Dateien wie mit dem klassischen youtube-upload.
2. Auto-Pipeline (-a --auto): Einmalige Batch-Verarbeitung eines Zielverzeichnisses mit FFmpeg-Splitting & Metadatenvererbung.
3. Dämon-Modus (-D --daemon): Dauerhafter Hintergrund-Dienst mit inotify-Überwachung.

SYSTEM-VORAUSSETZUNGEN:
- ffmpeg & ffprobe (im System-PATH vorhanden für Splitting & Thumbnail-Extraktion)
- python3-inotify (optional, aber empfohlen für den -D Dämon-Modus ohne Polling-Overhead)
"""

__title__ = "YouTube Video Uploader & CLI-Uploader"
__version__ = "1.1.0"

import argparse
import configparser
import glob
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import subprocess

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    # fcntl ist nur auf Unix verfügbar; auf anderen Plattformen läuft das Skript ohne Lockfile-Schutz weiter.
    HAS_FCNTL = False
import sys
import tempfile
import time
import unicodedata
import webbrowser
import requests
from datetime import datetime, timezone

# ------------------------------------------------------------------------------
# Debug-Modus über Umgebungsvariable 'DEBUG' setzen
# Prüft, ob 'DEBUG' in den Umgebungsvariablen aktiv gesetzt ist (true/yes/1).
# ------------------------------------------------------------------------------
DEBUG_MODE = os.environ.get('DEBUG', '').lower() in ('true', 'yes', '1')
os.environ['DEBUG'] = '1' if DEBUG_MODE else '0'

# ------------------------------------------------------------------------------
# Inotify Import-Prüfung (Echtzeit-Dateisystemüberwachung unter Linux)
# Versucht das pyinotify bzw. inotify Modul zu laden, um Polling zu vermeiden.
# ------------------------------------------------------------------------------
try:
    import inotify.adapters
    HAS_INOTIFY = True
except ImportError:
    inotify = None
    HAS_INOTIFY = False

# ==============================================================================
# KONFIGURATION & STANDARD-PFADE
# Pfade werden primär aus Umgebungsvariablen bezogen oder auf Default-Werte gesetzt.
# ==============================================================================
CONF_PATH = os.environ.get('CONFIG_FILE', '/etc/yt-upload/upload.conf')
CONF_D_DIR = os.environ.get('CONF_D_DIR', os.path.join(os.path.dirname(CONF_PATH), 'conf.d'))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Dynamic Path Detection: Docker Container Mounts (/videos) vs. Bare-Metal Host
IN_DIR = os.environ.get('IN_DIR', "/videos/in" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "in"))
WORK_DIR = os.environ.get('WORK_DIR', "/videos/work" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "work"))
DONE_DIR = os.environ.get('DONE_DIR', "/videos/done" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "done"))
CORRUPT_DIR = os.environ.get('CORRUPT_DIR', "/videos/corrupt" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "corrupt"))
# Für Dateien, bei denen bereits mind. ein Segment erfolgreich hochgeladen wurde, bevor ein Fehler auftrat.
# Getrennt von CORRUPT_DIR, damit kein versehentlicher Doppel-Upload bereits hochgeladener Segmente droht.
RETRY_DIR = os.environ.get('RETRY_DIR', "/videos/retry" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "retry"))

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


def _default_log_file():
    """
    Ermittelt den Standard-Logpfad:
    1. /log/upload.log, falls /log existiert (übliche Docker-Volume-Konvention)
    2. /var/log/yt-upload/yt-upload.log, falls beschreibbar (FHS-Standard für Bare-Metal-Daemons)
    3. BASE_DIR/upload.log als letzter Fallback (z.B. lokales Testen ohne Root-Rechte)
    """
    if os.path.exists("/log"):
        return "/log/upload.log"

    var_log_dir = "/var/log/yt-upload"
    try:
        os.makedirs(var_log_dir, exist_ok=True)
        if os.access(var_log_dir, os.W_OK):
            return os.path.join(var_log_dir, "yt-upload.log")
    except OSError:
        pass

    return os.path.join(BASE_DIR, "upload.log")


LOG_FILE = os.environ.get('LOG_FILE', _default_log_file())
CREDENTIALS_FILE = os.environ.get('CREDENTIALS_FILE', "/app/oauth/youtube-upload-credentials.json" if os.path.exists("/app/oauth") else os.path.join(BASE_DIR, "youtube-upload-credentials.json"))

# YouTube Limitierungen & Netzwerk-Chunk-Spezifikationen
SEGMENT_TIME_SEC = 36000     # Maximum 10 Stunden pro Video vor automatischem Splitting
CHUNK_UNIT_BYTES = 262144    # 256 KiB Basis-Einheit für Resumable Chunk Uploads (Zwingende YouTube API Vorgabe)

DEFAULT_DESCRIPTION = "Automatischer Upload via Script."
DEFAULT_TAGS = "Upload, Video"
DEFAULT_CATEGORY = "Entertainment"

VIDEO_PRIVACY = "unlisted"   # Mögliche Werte: 'public', 'private', 'unlisted'
VIDEO_LANGUAGE = "de"
ALLOW_EMBEDDING = True
PLAYLIST_NAME = ""

AUTO_GENERATE_THUMBNAIL = True
AUTO_THUMB_MIN_SEC = 15
AUTO_THUMB_MAX_SEC = 120
ALLOW_OVERWRITE = True
DYNAMIC_PLAYLISTS = False

# Laufzeit-Variablen für Hot-Reload und Zensur-Einstellungen
CURRENT_CONFIG_HASH = ""
ENABLE_DESCRIPTION_CENSOR = os.environ.get('ENABLE_DESCRIPTION_CENSOR', 'false').lower() in ('true', 'yes', '1')
_env_blacklist = os.environ.get('DESCRIPTION_BLACKLIST', '')
DESCRIPTION_BLACKLIST = [w.strip() for w in _env_blacklist.split(',') if w.strip()]


# ==============================================================================
# KONFIGURATIONSLADER & DYNAMISCHER HOT-RELOAD
# ==============================================================================
def load_configuration(log_changes=False):
    """
    Lädt die Konfiguration in folgender Hierarchie:
    1. Hardcoded Fallbacks
    2. Haupt-Konfiguration (/etc/yt-upload/upload.conf)
    3. Zusatz-Konfigurationen (/etc/yt-upload/conf.d/*.conf)
    4. Umgebungsvariablen (ergänzend/überschreibend)

    Berechnet einen MD5-Hash der Dateien für automatische Laufzeit-Aktualisierung (Hot-Reload).
    """
    global IN_DIR, WORK_DIR, DONE_DIR, CORRUPT_DIR, RETRY_DIR, LOG_FILE, CREDENTIALS_FILE
    global DEFAULT_DESCRIPTION, DEFAULT_TAGS, DEFAULT_CATEGORY, VIDEO_PRIVACY
    global VIDEO_LANGUAGE, ALLOW_EMBEDDING, PLAYLIST_NAME, AUTO_GENERATE_THUMBNAIL
    global AUTO_THUMB_MIN_SEC, AUTO_THUMB_MAX_SEC, ALLOW_OVERWRITE, DYNAMIC_PLAYLISTS
    global CURRENT_CONFIG_HASH
    global ENABLE_DESCRIPTION_CENSOR, DESCRIPTION_BLACKLIST

    in_dir = IN_DIR
    work_dir = WORK_DIR
    done_dir = DONE_DIR
    corrupt_dir = CORRUPT_DIR
    retry_dir = RETRY_DIR
    log_file = LOG_FILE
    credentials_file = CREDENTIALS_FILE

    default_description = DEFAULT_DESCRIPTION
    default_tags = DEFAULT_TAGS
    default_category = DEFAULT_CATEGORY
    video_privacy = VIDEO_PRIVACY
    video_language = VIDEO_LANGUAGE
    allow_embedding = ALLOW_EMBEDDING
    playlist_name = PLAYLIST_NAME
    auto_generate_thumbnail = AUTO_GENERATE_THUMBNAIL
    auto_thumb_min_sec = AUTO_THUMB_MIN_SEC
    auto_thumb_max_sec = AUTO_THUMB_MAX_SEC
    allow_overwrite = ALLOW_OVERWRITE
    enable_description_censor = ENABLE_DESCRIPTION_CENSOR
    dynamic_playlists = DYNAMIC_PLAYLISTS

    combined_blacklist = set(DESCRIPTION_BLACKLIST) if DESCRIPTION_BLACKLIST else set()

    config = configparser.ConfigParser()
    config_files = []

    # Prüfe ob Haupt-Konfigurationsdatei existiert
    if os.path.isfile(CONF_PATH):
        config_files.append(CONF_PATH)

    # Scanne conf.d Verzeichnis nach weiteren .conf Dateien
    if os.path.isdir(CONF_D_DIR):
        config_files.extend(sorted(glob.glob(os.path.join(CONF_D_DIR, "*.conf"))))

    # --- Hash-Berechnung für Laufzeit-Erkennung von Änderungen ---
    hasher = hashlib.md5()
    file_list_names = []
    for cfg in config_files:
        try:
            rel_name = os.path.relpath(cfg, os.path.dirname(CONF_PATH)) if CONF_PATH else os.path.basename(cfg)
            file_list_names.append(rel_name)
            hasher.update(cfg.encode('utf-8'))
            hasher.update(str(os.path.getmtime(cfg)).encode('utf-8'))
            with open(cfg, 'rb') as f:
                hasher.update(f.read())
        except OSError:
            pass

    # Protokolliere Änderungen falls sich der Hash unterscheidet
    new_hash = hasher.hexdigest()
    if log_changes and CURRENT_CONFIG_HASH and new_hash != CURRENT_CONFIG_HASH:
        files_str = ", ".join(file_list_names) if file_list_names else "conf.d"
        logging.info(f"🔄 Konfigurationsänderung erkannt (geändert: {files_str}). Synchronisiere...")
    CURRENT_CONFIG_HASH = new_hash

    # --- Step 1: Config-Dateien verarbeiten ---
    if config_files:
        config.read(config_files, encoding='utf-8')

        # Sektion [paths] einlesen
        if 'paths' in config:
            in_dir = config.get('paths', 'in_dir', fallback=in_dir)
            work_dir = config.get('paths', 'work_dir', fallback=work_dir)
            done_dir = config.get('paths', 'done_dir', fallback=done_dir)
            corrupt_dir = config.get('paths', 'corrupt_dir', fallback=corrupt_dir)
            retry_dir = config.get('paths', 'retry_dir', fallback=retry_dir)
            log_file = config.get('paths', 'log_file', fallback=log_file)
            credentials_file = config.get('paths', 'credentials_file', fallback=credentials_file)

        # Sektion [settings] einlesen
        if 'settings' in config:
            default_description = config.get('settings', 'default_description', fallback=default_description)
            default_tags = config.get('settings', 'default_tags', fallback=default_tags)
            default_category = config.get('settings', 'default_category', fallback=default_category)
            video_privacy = config.get('settings', 'privacy_status', fallback=video_privacy)
            video_language = config.get('settings', 'default_language', fallback=video_language)
            allow_embedding = config.getboolean('settings', 'allow_embedding', fallback=allow_embedding)
            playlist_name = config.get('settings', 'playlist_name', fallback=playlist_name)
            auto_generate_thumbnail = config.getboolean('settings', 'auto_generate_thumbnail', fallback=auto_generate_thumbnail)
            auto_thumb_min_sec = config.getint('settings', 'auto_thumb_min_sec', fallback=auto_thumb_min_sec)
            auto_thumb_max_sec = config.getint('settings', 'auto_thumb_max_sec', fallback=auto_thumb_max_sec)
            allow_overwrite = config.getboolean('settings', 'allow_overwrite', fallback=allow_overwrite)
            enable_description_censor = config.getboolean('settings', 'enable_description_censor', fallback=enable_description_censor)
            dynamic_playlists = config.getboolean('settings', 'enable_dynamic_playlists', fallback=dynamic_playlists)

        # Sektion [blacklist] einlesen
        if 'blacklist' in config:
            for key in config.options('blacklist'):
                if key.strip() != '__name__':
                    val = config.get('blacklist', key, fallback='true')
                    if val.lower() in ('true', '1', 'yes', 'on', ''):
                        combined_blacklist.add(key.strip().lower())

    # --- Step 2: Umgebungsvariablen verarbeiten (Höchste Priorität) ---
    IN_DIR = os.environ.get('IN_DIR', in_dir)
    WORK_DIR = os.environ.get('WORK_DIR', work_dir)
    DONE_DIR = os.environ.get('DONE_DIR', done_dir)
    CORRUPT_DIR = os.environ.get('CORRUPT_DIR', corrupt_dir)
    RETRY_DIR = os.environ.get('RETRY_DIR', retry_dir)
    LOG_FILE = os.environ.get('LOG_FILE', log_file)
    CREDENTIALS_FILE = os.environ.get('CREDENTIALS_FILE', credentials_file)

    VIDEO_PRIVACY = os.environ.get('VIDEO_PRIVACY', video_privacy)
    VIDEO_LANGUAGE = os.environ.get('VIDEO_LANGUAGE', video_language)
    PLAYLIST_NAME = os.environ.get('PLAYLIST_NAME', playlist_name)
    DEFAULT_DESCRIPTION = os.environ.get('DEFAULT_DESCRIPTION', default_description)
    DEFAULT_TAGS = os.environ.get('DEFAULT_TAGS', default_tags)
    DEFAULT_CATEGORY = os.environ.get('DEFAULT_CATEGORY', default_category)

    if 'ENABLE_DYNAMIC_PLAYLISTS' in os.environ:
        DYNAMIC_PLAYLISTS = os.environ['ENABLE_DYNAMIC_PLAYLISTS'].lower() in ('1', 'true', 'yes')
    else:
        DYNAMIC_PLAYLISTS = dynamic_playlists

    if 'ENABLE_DESCRIPTION_CENSOR' in os.environ:
        ENABLE_DESCRIPTION_CENSOR = os.environ['ENABLE_DESCRIPTION_CENSOR'].lower() in ('1', 'true', 'yes')
    else:
        ENABLE_DESCRIPTION_CENSOR = enable_description_censor

    if 'DESCRIPTION_BLACKLIST' in os.environ:
        _env_bl = os.environ['DESCRIPTION_BLACKLIST']
        for w in _env_bl.split(','):
            cleaned = w.strip().lower()
            if cleaned:
                combined_blacklist.add(cleaned)

    DESCRIPTION_BLACKLIST = sorted(list(combined_blacklist))

    ALLOW_EMBEDDING = allow_embedding
    AUTO_GENERATE_THUMBNAIL = auto_generate_thumbnail
    AUTO_THUMB_MIN_SEC = auto_thumb_min_sec
    AUTO_THUMB_MAX_SEC = auto_thumb_max_sec
    ALLOW_OVERWRITE = allow_overwrite

# Initiales Laden beim Scriptstart ausführen
load_configuration(log_changes=False)


# ==============================================================================
# OAUTH TOKEN HELPER
# ==============================================================================
def get_access_token(cred_file=None, client_secrets_file=None):
    """
    Generiert mittels Refresh-Token einen frischen OAuth2 Access Token bei Google.
    Liest dazu die Client-Credentials aus der JSON-Datei ein.
    """
    target_cred = cred_file or CREDENTIALS_FILE
    if not os.path.exists(target_cred):
        raise FileNotFoundError(f"Credentials-Datei nicht gefunden: {target_cred}")

    # Sicherheits-Check: Datei enthält Client-Secret & Refresh-Token im Klartext,
    # daher sollte sie nicht für Gruppe/Andere lesbar sein.
    try:
        current_mode = os.stat(target_cred).st_mode
        if current_mode & 0o077:
            try:
                os.chmod(target_cred, 0o600)
                logging.warning(f"Credentials-Datei {target_cred} war zu offen berechtigt, auf 600 korrigiert.")
            except OSError as chmod_err:
                logging.warning(f"Credentials-Datei {target_cred} ist zu offen berechtigt und konnte nicht korrigiert werden: {chmod_err}")
    except OSError:
        pass

    with open(target_cred, "r", encoding="utf-8") as f:
        data = json.load(f)

    client_data = data
    if client_secrets_file:
        with open(client_secrets_file, "r", encoding="utf-8") as f:
            client_data = json.load(f)

    # Extrahiere IDs aus verschiedenen möglichen JSON-Strukturen (installed/web/flat)
    client_id = client_data.get("client_id") or client_data.get("installed", {}).get("client_id") or client_data.get("web", {}).get("client_id")
    client_secret = client_data.get("client_secret") or client_data.get("installed", {}).get("client_secret") or client_data.get("web", {}).get("client_secret")
    refresh_token = data.get("refresh_token")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(f"OAuth-Credentials in {target_cred} unvollständig (client_id, client_secret oder refresh_token fehlt).")

    # Sende Token-Refresh Request an Google OAuth 2.0 Endpoint
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    res = requests.post(token_url, data=payload, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"Fehler beim Erneuern des Access Tokens: {res.text}")

    return res.json().get("access_token")


# ==============================================================================
# DATEISYSTEM & REINIGUNGS-HELFER
# ==============================================================================
def ensure_directories():
    """Stellt sicher, dass alle notwendigen Zielverzeichnisse auf dem Dateisystem existieren."""
    for d in [IN_DIR, WORK_DIR, DONE_DIR, CORRUPT_DIR, RETRY_DIR]:
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
    """Bestimmt das Ziel basierend auf der ALLOW_OVERWRITE Einstellung."""
    candidate = os.path.join(directory, filename)
    if ALLOW_OVERWRITE:
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
        with open(progress_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logging.warning(f"Konnte Progress-Datei {progress_path} nicht lesen, starte ohne Fortschritt: {e}")
        return {}


def save_segment_progress(work_path, progress):
    """Persistiert den aktuellen Fortschritt sofort nach jedem erfolgreichen Segment-Upload."""
    progress_path = _progress_file_path(work_path)
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(progress, f)
    except OSError as e:
        logging.warning(f"Konnte Progress-Datei {progress_path} nicht schreiben: {e}")


def clear_segment_progress(work_path):
    """Entfernt die Progress-Datei nach vollständigem Erfolg (oder wenn kein Segment fertig wurde)."""
    progress_path = _progress_file_path(work_path)
    try:
        if os.path.isfile(progress_path):
            os.unlink(progress_path)
    except OSError as e:
        logging.warning(f"Konnte Progress-Datei {progress_path} nicht löschen: {e}")


def cleanup_work_files(work_paths, is_error=False):
    """Löscht temporär erzeugte Arbeits- oder Segmentdateien aus dem WORK-Ordner."""
    log_func = logging.warning if is_error else logging.info
    log_func("Bereinige Dateien des aktuellen Jobs im WORK-Verzeichnis...")
    for item_path in work_paths:
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.unlink(item_path)
        except OSError as e:
            logging.error(f"Fehler beim Löschen von {item_path}: {e}")


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
        logging.warning(f"Generiertes Thumbnail kann nicht gelöscht werden ({candidate}): {e}")


def is_file_ready_and_valid(file_path, wait_interval=3, max_checks=10):
    """
    Prüft, ob eine Datei vollständig geschrieben wurde (Größenprüfung über Zeit)
    und ob es sich um einen fehlerfreien Video-Stream handelt (mittels ffprobe).
    """
    if not os.path.exists(file_path):
        return False

    last_size = -1
    # Prüfe iterativ, ob die Datei noch von einem externen Prozess geschrieben wird
    for check_num in range(max_checks):
        try:
            current_size = os.path.getsize(file_path)
        except OSError as e:
            logging.error(f"Fehler bei Dateigrößenprüfung von {file_path}: {e}")
            return False

        if current_size == 0:
            logging.warning(f"Datei ist noch 0 Bytes groß, warte... ({check_num + 1}/{max_checks})")
            time.sleep(wait_interval)
            continue

        if last_size != -1 and current_size == last_size:
            break
        elif last_size != -1:
            logging.info(f"Datei wächst noch ({current_size / (1024*1024):.1f} MB)... warte weiter.")

        last_size = current_size
        time.sleep(wait_interval)
    else:
        logging.warning(f"Datei wird nach {max_checks * wait_interval}s noch geschrieben: {os.path.basename(file_path)}")
        return False

    # Integritätsprüfung via ffprobe durchführen
    cmd_check = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    res = subprocess.run(cmd_check, capture_output=True, text=True)
    if res.returncode != 0:
        logging.warning(f"FFprobe-Check fehlgeschlagen (evtl. unvollständig oder ungültiges Format): {os.path.basename(file_path)}")
        return False

    return True


# ==============================================================================
# INOTIFY & EINGANGSÜBERWACHUNG
# ==============================================================================
def find_existing_video(target_dir=IN_DIR):
    """Sucht rekursiv nach kompatiblen Videodateien im Eingangsverzeichnis."""
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")
    if not os.path.isdir(target_dir):
        return None
    for root, _, files in os.walk(target_dir):
        for file in files:
            if file.lower().endswith(valid_exts):
                return os.path.join(root, file)
    return None


def wait_for_input(target_dir=IN_DIR, inotify_adapter=None):
    """
    Blockiert den Prozess im Dämonenmodus, bis eine neue Datei per inotify signalisiert
    oder per Polling-Fallback im Eingangsordner gefunden wird.
    """
    load_configuration(log_changes=True)

    # Prüfe zuerst, ob bereits verarbeitbare Dateien im Ordner liegen
    existing = find_existing_video(target_dir)
    if existing:
        logging.info(f"Bestehende Datei gefunden: {existing}")
        return existing

    logging.info(f"Warte via inotify auf neue Dateien in {target_dir}...")
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")

    # Fallback-Schleife falls inotify nicht im System geladen ist
    if not HAS_INOTIFY or inotify_adapter is None:
        if not HAS_INOTIFY:
            logging.warning("inotify-Modul nicht verfügbar, nutze Polling-Fallback.")
        while True:
            time.sleep(10)
            load_configuration(log_changes=True)
            existing = find_existing_video(target_dir)
            if existing:
                return existing

    # Event-Loop für inotify
    while True:
        try:
            for event in inotify_adapter.event_gen(yield_nones=False, timeout_s=10):
                load_configuration(log_changes=True)

                if event is None:
                    existing = find_existing_video(target_dir)
                    if existing:
                        logging.info(f"Datei via Fallback-Timer erkannt: {existing}")
                        return existing
                    continue

                (_, type_names, path, filename) = event
                # Reagiere auf abgeschlossene Schreibvorgänge oder Verschiebungen
                if any(t in type_names for t in ["IN_CLOSE_WRITE", "IN_MOVED_TO"]):
                    if filename.lower().endswith(valid_exts):
                        full_path = os.path.join(path, filename)
                        if os.path.isfile(full_path):
                            logging.info(f"Datei erfolgreich via inotify erkannt: {full_path}")
                            return full_path
        except Exception as e:
            logging.error(f"Fehler beim Inotify-Observer: {e}")
            time.sleep(5)
            existing = find_existing_video(target_dir)
            if existing:
                return existing


# ==============================================================================
# METADATEN-PARSING & THUMBNAIL-EXTRAKTION
# ==============================================================================
def truncate_title(title: str, max_length: int = 100) -> str:
    """Kürzt Titel unter Einhaltung des YouTube-Limits von 100 Zeichen."""
    if not title:
        return ""
    if len(title) <= max_length:
        return title
    return title[: max_length - 3].rstrip() + "..."


def get_valid_category_id(category_input):
    """Mappt Kategorie-Namen auf die offiziellen numerischen YouTube-IDs."""
    if not category_input:
        return "22"

    if str(category_input).isdigit():
        return str(category_input)

    cat_lower = str(category_input).lower()
    mapping = {
        "film & animation": "1",
        "autos & vehicles": "2",
        "music": "10",
        "pets & animals": "15",
        "sports": "17",
        "short movies": "18",
        "travel & events": "19",
        "gaming": "20",
        "videoblogging": "21",
        "people & blogs": "22",
        "comedy": "23",
        "entertainment": "24",
        "news & politics": "25",
        "howto & style": "26",
        "education": "27",
        "science & technology": "28",
        "nonprofits & activism": "29",
        "movies": "30",
        "anime/animation": "31",
        "action/adventure": "32",
        "classics": "33",
        "documentary": "35",
        "drama": "36",
        "family": "37",
        "foreign": "38",
        "horror": "39",
        "sci-fi/fantasy": "40",
        "thriller": "41",
        "shorts": "42",
        "shows": "43",
        "trailers": "44",
    }

    for key, val in mapping.items():
        if key in cat_lower:
            return val

    return "22"


def normalize_recording_date(value):
    """Konvertiert Datumsangaben in das ISO-8601 UTC-Format für die API."""
    if not value:
        return None

    value = str(value).strip()

    if re.fullmatch(r"\d{8}", value):
        parsed = datetime.strptime(value, "%Y%m%d")
        return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    return value


def sanitize_text(text):
    """Entfernt Steuerzeichen und ungültige Klammern (<, >) für die YouTube API."""
    if not text:
        return text

    # Kurzform <3 in Herz umwandeln
    text = text.replace("<3", "♥")

    # Spitzzeichen für die API entfernen
    text = text.replace("<", "").replace(">", "")

    # Steuerzeichen entfernen, Unicode-Symbole & Umlaute beibehalten
    cleaned_chars = [c for c in text if unicodedata.category(c) != 'Cc']
    result = ''.join(cleaned_chars)
    return re.sub(r'\s+', ' ', result).strip()


def parse_location(location_str):
    """Verarbeitet Geokoordinaten aus String-Formaten (latitude=X,longitude=Y)."""
    if not location_str:
        return None
    try:
        parts = dict(item.split('=') for item in location_str.split(','))
        loc = {
            "latitude": float(parts["latitude"]),
            "longitude": float(parts["longitude"])
        }
        if "altitude" in parts:
            loc["altitude"] = float(parts["altitude"])
        return loc
    except Exception as e:
        logging.warning(f"Konnte Location-String nicht parsen ('{location_str}'): {e}")
        return None


def extract_metadata_and_thumb(file_path):
    """
    Extrahiert eingebettete Tags (Titel, Beschreibung, Künstler etc.) via ffprobe
    sowie embedded Coverart/Thumbnails. Generiert alternativ ein Vorschaubild aus dem Video.
    """
    metadata = {
        "title": None,
        "description": None,
        "purl": None,
        "genre": None,
        "date": None,
        "artist": None,
        "thumb_path": None,
        "duration": 0,
    }

    # Metadaten aus dem Container via FFprobe extrahieren
    try:
        cmd_probe = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path
        ]
        res = subprocess.run(cmd_probe, capture_output=True, text=True, check=True)
        probe_data = json.loads(res.stdout)

        format_info = probe_data.get("format", {})
        tags = {str(k).lower(): v for k, v in format_info.get("tags", {}).items()}

        if "duration" in format_info:
            metadata["duration"] = int(float(format_info["duration"]))

        raw_title = tags.get("title")
        if raw_title:
            metadata["title"] = truncate_title(sanitize_text(raw_title), max_length=100)

        metadata["description"] = tags.get("description") or tags.get("comment")
        metadata["purl"] = tags.get("purl")
        metadata["genre"] = tags.get("genre")
        metadata["date"] = tags.get("date")
        metadata["artist"] = sanitize_text(tags.get("artist") or tags.get("album_artist"))

    except Exception as e:
        logging.error(f"Fehler beim Auslesen der Metadaten via FFprobe: {e}")

    temp_dir = tempfile.gettempdir()
    pid = os.getpid()
    timestamp = int(time.time() * 1000)
    thumb_path = os.path.join(temp_dir, f"yt_thumb_{pid}_{timestamp}.jpg")
    temp_attach = os.path.join(temp_dir, f"yt_attach_{pid}_{timestamp}.jpg")

    # Versuche eingebettete Cover-Bilder zu extrahieren (MKV / MP4 Attachments)
    try:
        cmd_mkv = ["ffmpeg", "-y", "-dump_attachment:t:0", temp_attach, "-i", file_path]
        subprocess.run(cmd_mkv, capture_output=True, text=True)

        if not os.path.exists(temp_attach) or os.path.getsize(temp_attach) == 0:
            cmd_mp4 = [
                "ffmpeg", "-y", "-i", file_path,
                "-map", "0:v:m:attached_pic:0?", "-c", "copy", temp_attach
            ]
            subprocess.run(cmd_mp4, capture_output=True, text=True)

        if os.path.exists(temp_attach) and os.path.getsize(temp_attach) > 0:
            cmd_conv = ["ffmpeg", "-y", "-i", temp_attach, "-q:v", "2", thumb_path]
            subprocess.run(cmd_conv, capture_output=True, text=True)

        # Fallback: Frame an bestimmter Position im Video als Thumbnail rendern
        if (not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0) and AUTO_GENERATE_THUMBNAIL:
            duration = metadata["duration"]
            
            if duration <= 0:
                seek_sec = AUTO_THUMB_MIN_SEC
            elif duration < AUTO_THUMB_MIN_SEC:
                seek_sec = max(1, duration // 2)
            else:
                target_sec = AUTO_THUMB_MIN_SEC + ((AUTO_THUMB_MAX_SEC - AUTO_THUMB_MIN_SEC) // 2)
                seek_sec = min(target_sec, max(1, duration - 1))

            logging.info(f"Generiere Auto-Thumbnail bei Sekunde {seek_sec} (Video-Dauer: {duration}s)...")

            cmd_frame = [
                "ffmpeg", "-y",
                "-ss", str(seek_sec),
                "-i", file_path,
                "-frames:v", "1",
                "-q:v", "2",
                thumb_path
            ]
            subprocess.run(cmd_frame, capture_output=True, text=True)

            if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
                logging.warning("Seek fehlgeschlagen, versuche ersten Keyframe zu greifen...")
                cmd_keyframe = [
                    "ffmpeg", "-y",
                    "-discard", "nokey",
                    "-i", file_path,
                    "-frames:v", "1",
                    "-q:v", "2",
                    thumb_path
                ]
                subprocess.run(cmd_keyframe, capture_output=True, text=True)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            metadata["thumb_path"] = thumb_path
            logging.info(f"Thumbnail erfolgreich zugewiesen: {thumb_path}")
        else:
            logging.warning("Kein Thumbnail generiert/gefunden. YouTube wird ein automatisches Frame wählen.")

    except Exception as e:
        logging.warning(f"Fehler bei der Thumbnail-Extraktion: {e}")
    finally:
        # Aufräumen der temporären Attachment-Datei
        if os.path.exists(temp_attach):
            try:
                os.remove(temp_attach)
            except OSError:
                pass

    return metadata


# ==============================================================================
# BESCHREIBUNGS- UND TITEL-ZENSUR
# ==============================================================================
def censor_text(text: str) -> str:
    """
    Durchsucht den übergebenen Text nach Wörtern aus der Konfigurations-Blacklist
    und ersetzt gefundene Treffer durch Sternchen (*), unter Beibehaltung der Wortlänge.
    """
    if not ENABLE_DESCRIPTION_CENSOR or not text:
        return text

    if DESCRIPTION_BLACKLIST:
        escaped_words = [re.escape(word) for word in DESCRIPTION_BLACKLIST if word.strip()]
        if escaped_words:
            pattern = re.compile(r'(?i)' + '|'.join(escaped_words))

            def replace_match(match):
                matched_str = match.group(0)
                return '*' * len(matched_str)

            text = pattern.sub(replace_match, text)

    return text


# ==============================================================================
# FFMPEG LOSSLESS SPLITTER
# ==============================================================================
def split_video_if_needed(work_path):
    """
    Prüft, ob das Video die Maximallänge (10h) überschreitet.
    Wenn ja, wird es verlustfrei mittels Stream-Copying (-c copy) in mehrere Teile gesplittet.
    """
    try:
        cmd_probe = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            work_path
        ]
        res = subprocess.run(cmd_probe, capture_output=True, text=True, check=True)
        duration_sec = float(res.stdout.strip())
    except Exception as e:
        logging.error(f"Fehler bei Dauer-Ermittlung: {e}")
        return [work_path]

    logging.info(f"Videolänge: {int(duration_sec)} Sekunden ({duration_sec / 3600:.2f} Stunden)")

    if duration_sec <= SEGMENT_TIME_SEC:
        return [work_path]

    logging.info("Video überschreitet 10 Stunden. Starte FFmpeg-Splitting...")
    filename = os.path.basename(work_path)
    base_name, ext = os.path.splitext(filename)
    segment_pattern = os.path.join(WORK_DIR, f"{base_name}_part%02d{ext}")

    # Alte Segmente entfernen, falls noch vorhanden
    for item in os.listdir(WORK_DIR):
        if item.startswith(f"{base_name}_part") and item.endswith(ext):
            stale_segment = os.path.join(WORK_DIR, item)
            try:
                os.remove(stale_segment)
            except OSError as e:
                raise RuntimeError(f"Altes Segment kann nicht gelöscht werden: {stale_segment}") from e

    # Segmentierung verlustfrei mit Stream-Copy ausführen
    cmd_split = [
        "ffmpeg", "-y", "-i", work_path, "-c", "copy", "-map", "0",
        "-avoid_negative_ts", "make_zero", "-f", "segment",
        "-segment_time", str(SEGMENT_TIME_SEC), "-reset_timestamps", "1",
        segment_pattern
    ]

    try:
        subprocess.run(cmd_split, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg Splitting fehlgeschlagen: {e.stderr}")
        raise

    # Generierte Segmente einsammeln
    created_segments = []
    part_idx = 0
    while True:
        seg_candidate = os.path.join(WORK_DIR, f"{base_name}_part{part_idx:02d}{ext}")
        if os.path.exists(seg_candidate):
            created_segments.append(seg_candidate)
            part_idx += 1
        else:
            break

    if not created_segments:
        raise RuntimeError("FFmpeg hat keine Videosegmente erzeugt.")

    return created_segments


class PermanentUploadError(RuntimeError):
    """Wird bei dauerhaften (nicht behebbaren) API-Fehlern ausgelöst, um sinnloses Retrying zu vermeiden."""
    pass


# ==============================================================================
# REST API UPLOADER & PLAYLIST MANAGEMENT
# ==============================================================================
def add_video_to_playlist(video_id, playlist_name, access_token, privacy=VIDEO_PRIVACY,
                           cred_file=None, client_secrets_file=None):
    """
    Sucht eine Playlist anhand ihres Namens. Erstellt diese, falls nicht vorhanden,
    und fügt das hochgeladene Video hinzu (inkl. Retry-Logik bei temporären API-Fehlern).
    Erneuert den Access-Token automatisch bei 401/403 (z.B. nach sehr langen Uploads).
    """
    if not playlist_name or not video_id:
        return False

    headers = {"Authorization": f"Bearer {access_token}"}

    def _refresh_token_if_possible():
        """Versucht, den Token zu erneuern; gibt True zurück, wenn headers aktualisiert wurden."""
        try:
            new_token = get_access_token(cred_file, client_secrets_file)
            headers["Authorization"] = f"Bearer {new_token}"
            return True
        except Exception as e:
            logging.warning(f"Token-Refresh für Playlist-Zuweisung fehlgeschlagen: {e}")
            return False

    try:
        playlist_id = None
        next_page = None
        auth_retry_used = False

        # Durchsuche bestehende Playlists des Nutzers
        while True:
            list_url = f"https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50"
            if next_page:
                list_url += f"&pageToken={next_page}"

            res = requests.get(list_url, headers=headers, timeout=30)

            if res.status_code in (401, 403) and not auth_retry_used:
                logging.warning("Playlist-Abruf: Token abgelaufen, erneuere und versuche erneut...")
                auth_retry_used = True
                if _refresh_token_if_possible():
                    continue
                logging.warning(f"Konnte Playlists nicht abrufen ({res.status_code}): {res.text}")
                break

            if res.status_code == 200:
                data = res.json()
                for item in data.get("items", []):
                    if item["snippet"]["title"].lower() == playlist_name.lower():
                        playlist_id = item["id"]
                        break
                if playlist_id:
                    break
                next_page = data.get("nextPageToken")
                if not next_page:
                    break
            else:
                logging.warning(f"Konnte Playlists nicht abrufen ({res.status_code}): {res.text}")
                break

        # Falls Playlist nicht existiert, erstelle sie neu
        if not playlist_id:
            create_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet,status"
            create_body = {
                "snippet": {"title": playlist_name, "description": "Automatisch erstellt"},
                "status": {"privacyStatus": privacy}
            }
            create_res = requests.post(create_url, headers=headers, json=create_body, timeout=30)
            if create_res.status_code in (401, 403) and not auth_retry_used:
                logging.warning("Playlist-Erstellung: Token abgelaufen, erneuere und versuche erneut...")
                auth_retry_used = True
                if _refresh_token_if_possible():
                    create_res = requests.post(create_url, headers=headers, json=create_body, timeout=30)
            if create_res.status_code in (200, 201):
                playlist_id = create_res.json().get("id")
            elif create_res.status_code not in (200, 201):
                logging.warning(f"Konnte Playlist nicht erstellen ({create_res.status_code}): {create_res.text}")

        # Video der Playlist zuweisen
        if playlist_id:
            item_url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
            item_body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id}
                }
            }

            max_retries = 3
            retry_delay = 3

            for attempt in range(1, max_retries + 1):
                item_res = requests.post(item_url, headers=headers, json=item_body, timeout=30)

                if item_res.status_code in (401, 403) and not auth_retry_used:
                    logging.warning("Playlist-Zuweisung: Token abgelaufen, erneuere und versuche erneut...")
                    auth_retry_used = True
                    if _refresh_token_if_possible():
                        continue

                if item_res.status_code in (200, 201):
                    logging.info(f"Video {video_id} erfolgreich zur Playlist '{playlist_name}' hinzugefügt.")
                    return True

                if item_res.status_code in (409, 500, 502, 503, 504) and attempt < max_retries:
                    logging.warning(
                        f"YouTube API meldet {item_res.status_code} beim Playlist-Assignment. "
                        f"Retry {attempt}/{max_retries} in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logging.warning(f"Video konnte Playlist nicht hinzugefügt werden: {item_res.text}")
                    break

    except Exception as e:
        logging.error(f"Fehler bei Playlist-API: {e}")
    return False


def upload_single_video(
    file_path,
    title,
    desc,
    category,
    tags,
    rec_date,
    thumb_path,
    playlist_name,
    privacy=VIDEO_PRIVACY,
    publish_at=None,
    license_type="youtube",
    location=None,
    default_lang=VIDEO_LANGUAGE,
    default_audio_lang=VIDEO_LANGUAGE,
    embeddable=ALLOW_EMBEDDING,
    cred_file=None,
    client_secrets_file=None,
    chunksize=268435456,
    open_link=False
):
    """
    Führt den eigentlichen Upload über die Resumable Upload HTTP REST API von YouTube durch.
    Überträgt Metadaten, Chunks, gesetzte Thumbnails und verknüpft Playlists.
    """
    if title:
        title = truncate_title(sanitize_text(title), max_length=100)
    else:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        title = truncate_title(sanitize_text(base_name), max_length=100)

    if desc:
        desc = desc[:5000]

    if publish_at and privacy != "private":
        logging.info(f"Status wurde für geplanten Upload von '{privacy}' auf 'private' korrigiert.")
        privacy = "private"

    logging.info(f"Lade hoch via native HTTP REST API ({privacy}): {os.path.basename(file_path)}")

    file_size = os.path.getsize(file_path)
    access_token = get_access_token(cred_file, client_secrets_file)

    session = requests.Session()

    # Ausrichtung der Chunksize an die von YouTube vorgeschriebene 256-KiB-Grenze
    if chunksize % CHUNK_UNIT_BYTES != 0:
        adjusted_chunksize = max(CHUNK_UNIT_BYTES, (chunksize // CHUNK_UNIT_BYTES) * CHUNK_UNIT_BYTES)
        logging.info(f"Chunksize angepasst auf Vielfaches von 256 KiB: {chunksize} -> {adjusted_chunksize} Bytes")
        chunksize = adjusted_chunksize

    # Säuberung und Kürzung der Tags
    clean_tags = []
    if isinstance(tags, list):
        tags_list = tags
    elif isinstance(tags, str) and tags:
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        tags_list = []

    for tag in tags_list:
        sanitized_tag = sanitize_text(str(tag)).strip()
        if sanitized_tag and len(sanitized_tag) <= 100:
            clean_tags.append(sanitized_tag)
    
    final_tags = []
    current_length = 0
    for t in clean_tags:
        if current_length + len(t) + 1 <= 400:
            final_tags.append(t)
            current_length += len(t) + 1

    cat_id = get_valid_category_id(category)
    if not cat_id:
        cat_id = "22"

    # Erstelle API Metadaten Structure
    snippet = {
        "title": title,
        "description": desc or "",
        "categoryId": str(cat_id),
    }
    
    if final_tags:
        snippet["tags"] = final_tags

    if default_lang:
        snippet["defaultLanguage"] = default_lang
    if default_audio_lang:
        snippet["defaultAudioLanguage"] = default_audio_lang

    status = {
        "privacyStatus": privacy,
        "embeddable": embeddable,
        "license": license_type,
    }
    if publish_at:
        status["publishAt"] = publish_at

    metadata_body = {
        "snippet": snippet,
        "status": status,
    }

    if rec_date:
        rec_date = normalize_recording_date(rec_date)
        metadata_body["recordingDetails"] = {"recordingDate": rec_date}

    parsed_loc = parse_location(location) if isinstance(location, str) else location
    if parsed_loc:
        metadata_body["recordingDetails"] = metadata_body.get("recordingDetails", {})
        metadata_body["recordingDetails"]["location"] = parsed_loc

    logging.debug(f"PAYLOAD DEBUG: {json.dumps(metadata_body, ensure_ascii=False)}")

    parts = "snippet,status"
    if "recordingDetails" in metadata_body:
        parts += ",recordingDetails"

    # 1. Resumable Upload Session bei Google initialisieren
    init_url = f"https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part={parts}"
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/*"
    }

    logging.info("Initialisiere Resumable Upload Session...")

    init_res = None
    init_max_retries = 3
    for init_attempt in range(1, init_max_retries + 1):
        try:
            init_res = session.post(init_url, headers=init_headers, json=metadata_body, timeout=60)
        except requests.exceptions.RequestException as e:
            logging.warning(f"Netzwerkfehler bei Session-Init (Versuch {init_attempt}/{init_max_retries}): {e}")
            if init_attempt < init_max_retries:
                time.sleep(2 ** init_attempt)
                continue
            raise RuntimeError(f"Session-Init nach {init_max_retries} Versuchen fehlgeschlagen: {e}") from e

        if init_res.status_code == 200:
            break

        # Token abgelaufen: einmalig erneuern und erneut versuchen
        if init_res.status_code in (401, 403) and init_attempt < init_max_retries:
            access_token = get_access_token(cred_file, client_secrets_file)
            init_headers["Authorization"] = f"Bearer {access_token}"
            continue

        # Transiente Server-Fehler: mit Backoff erneut versuchen
        if init_res.status_code in (500, 502, 503, 504) and init_attempt < init_max_retries:
            logging.warning(
                f"YouTube API meldet {init_res.status_code} bei Session-Init. "
                f"Retry {init_attempt}/{init_max_retries} in {2 ** init_attempt}s..."
            )
            time.sleep(2 ** init_attempt)
            continue

        raise RuntimeError(f"Session-Init fehlgeschlagen ({init_res.status_code}): {init_res.text}")

    upload_url = init_res.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Keine Upload-Location im Header erhalten.")

    logging.info(f"Starte Chunk-Upload ({file_size / (1024 * 1024):.2f} MB) mit Chunksize {chunksize / (1024 * 1024):.1f} MB...")

    max_retries = 5
    video_id = None
    uploaded_bytes = 0

    # 2. Datei in Chunks unterteilt übertragen
    with open(file_path, "rb") as f:
        while uploaded_bytes < file_size:
            chunk_start = uploaded_bytes
            f.seek(chunk_start)
            chunk = f.read(chunksize)
            chunk_len = len(chunk)
            chunk_end = chunk_start + chunk_len - 1

            chunk_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Range": f"bytes {chunk_start}-{chunk_end}/{file_size}",
                "Content-Type": "video/*"
            }

            chunk_success = False
            for attempt in range(1, max_retries + 1):
                try:
                    put_res = session.put(upload_url, headers=chunk_headers, data=chunk, timeout=120)

                    # Status 200/201: Upload vollständig abgeschlossen
                    if put_res.status_code in (200, 201):
                        resp_data = put_res.json()
                        video_id = resp_data.get("id")
                        uploaded_bytes = file_size
                        chunk_success = True
                        logging.info(f"Upload ERFOLGREICH abgeschlossen! Video-ID: {video_id}")
                        break

                    # Status 308: Incomplete, Chunk erfolgreich empfangen (mit robuster Offset-Auswertung)
                    elif put_res.status_code == 308:
                        range_hdr = put_res.headers.get("Range")
                        if range_hdr and "-" in range_hdr:
                            try:
                                uploaded_bytes = int(range_hdr.split("-")[1]) + 1
                            except (ValueError, IndexError):
                                uploaded_bytes += chunk_len
                        else:
                            uploaded_bytes += chunk_len

                        if uploaded_bytes < file_size and uploaded_bytes % CHUNK_UNIT_BYTES != 0:
                            uploaded_bytes = (uploaded_bytes // CHUNK_UNIT_BYTES) * CHUNK_UNIT_BYTES
                            logging.warning(f"Offset korrigiert auf 256-KiB-Grenze: {uploaded_bytes} Bytes")

                        pct = (uploaded_bytes / file_size) * 100
                        logging.info(f"Fortschritt: {uploaded_bytes / (1024*1024):.1f} / {file_size / (1024*1024):.1f} MB ({pct:.1f}%)")
                        chunk_success = True
                        break

                    # Token abgelaufen: Access Token erneuern und erneut versuchen
                    elif put_res.status_code in (401, 403):
                        access_token = get_access_token(cred_file, client_secrets_file)
                        chunk_headers["Authorization"] = f"Bearer {access_token}"
                        raise RuntimeError("Token erneuert, versuche Chunk erneut...")

                    # Alle übrigen Status-Codes: immer loggen, damit Fehler nicht stillschweigend verschwinden
                    else:
                        logging.error(
                            f"Unerwarteter Status {put_res.status_code} beim Chunk-Upload "
                            f"(Versuch {attempt}/{max_retries}): {put_res.text[:500]}"
                        )
                        # Dauerhafte Client-Fehler (z.B. 400 ungültige Metadaten, 404 Session weg)
                        # lassen sich durch Wiederholen nicht beheben -> sofort abbrechen statt 5x zu retryen
                        if 400 <= put_res.status_code < 500 and put_res.status_code not in (401, 403, 408, 429):
                            raise PermanentUploadError(
                                f"Nicht behebbarer Fehler ({put_res.status_code}): {put_res.text[:500]}"
                            )
                        raise RuntimeError(f"Transienter Fehler ({put_res.status_code}), versuche erneut...")

                except PermanentUploadError:
                    raise  # nicht abfangen/retryen - direkt an den Aufrufer durchreichen

                except Exception as e:
                    logging.warning(f"Fehler bei Chunk-Upload (Versuch {attempt}/{max_retries}): {e}")
                    time.sleep(2 ** attempt)

            if not chunk_success:
                raise RuntimeError("Max Retries beim Upload überschritten.")

    # 3. Post-Upload-Schritte: Thumbnail hochladen und Playlist-Zuweisung
    # Bei sehr großen Dateien kann der Chunk-Upload allein schon die Token-Lebensdauer
    # (~1h) überschreiten. Token hier proaktiv erneuern statt erst bei 401 zu reagieren.
    try:
        access_token = get_access_token(cred_file, client_secrets_file)
    except Exception as refresh_err:
        logging.warning(f"Token-Refresh vor Post-Upload-Schritten fehlgeschlagen, verwende bestehenden Token: {refresh_err}")

    try:
        if video_id:
            if thumb_path and os.path.exists(thumb_path):
                try:
                    logging.info(f"Lade benutzerdefiniertes Thumbnail hoch: {thumb_path}")
                    thumb_url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
                    with open(thumb_path, "rb") as tf:
                        thumb_bytes = tf.read()

                    for thumb_attempt in (1, 2):
                        t_res = session.post(
                            thumb_url,
                            headers={
                                "Authorization": f"Bearer {access_token}",
                                "Content-Type": "image/jpeg"
                            },
                            data=thumb_bytes,
                            timeout=60
                        )
                        if t_res.status_code in (200, 201):
                            logging.info("Thumbnail erfolgreich gesetzt.")
                            break
                        elif t_res.status_code in (401, 403) and thumb_attempt == 1:
                            logging.warning("Thumbnail-Upload: Token abgelaufen, erneuere und versuche erneut...")
                            access_token = get_access_token(cred_file, client_secrets_file)
                            continue
                        else:
                            logging.warning(f"Thumbnail-Upload fehlgeschlagen ({t_res.status_code}): {t_res.text}")
                            break
                except Exception as te:
                    logging.warning(f"Fehler beim Thumbnail-Setzen: {te}")

            if playlist_name:
                add_video_to_playlist(
                    video_id, playlist_name, access_token, privacy,
                    cred_file=cred_file, client_secrets_file=client_secrets_file
                )

            if open_link:
                v_url = f"https://www.youtube.com/watch?v={video_id}"
                logging.info(f"Öffne Browser-Link: {v_url}")
                webbrowser.open(v_url)
    finally:
        cleanup_generated_thumbnail(thumb_path)

    return video_id


# ==============================================================================
# PIPELINE STEUERUNG FÜR EINZELDATEIEN
# ==============================================================================
def process_single_file(file_path, args=None):
    """
    Steuert die vollständige Verarbeitung einer Datei:
    1. Validierung
    2. Verschieben nach WORK
    3. Metadaten-Extraktion & Zensierung
    4. Ggf. Splitting bei Überlänge
    5. Upload aller Teile
    6. Verschieben nach DONE (oder CORRUPT bei Fehler)
    """
    filename = os.path.basename(file_path)
    logging.info(f"--- VERARBEITE DATEI: {filename} ---")

    # Vorab-Prüfung auf Integrität der Quelldatei
    if not is_file_ready_and_valid(file_path):
        logging.error(f"Datei unvollständig oder ungültig: {filename}. Verschiebe nach CORRUPT...")
        target_corrupt = resolve_target_path(CORRUPT_DIR, filename)
        shutil.move(file_path, target_corrupt)
        return

    work_path = resolve_target_path(WORK_DIR, filename)
    logging.info(f"Verschiebe nach WORK: {work_path}")
    shutil.move(file_path, work_path)

    meta = extract_metadata_and_thumb(work_path)

    # Zusammenführen von Argumenten und Container-Metadaten
    raw_title = (args.title if args and hasattr(args, 'title') and args.title else meta["title"]) or os.path.splitext(filename)[0]
    raw_desc = (args.description if args and hasattr(args, 'description') and args.description else meta["description"]) or DEFAULT_DESCRIPTION

    raw_desc = sanitize_text(raw_desc)

    if meta.get("purl") and meta["purl"] not in raw_desc:
        raw_desc = f"{raw_desc}\n\nQuelle: {meta['purl']}"

    # Zensur-Filter anwenden
    title_base = censor_text(raw_title)
    desc_base = censor_text(raw_desc)

    category = (args.category if args and hasattr(args, 'category') and args.category else meta["genre"]) or DEFAULT_CATEGORY
    tags = (args.tags if args and hasattr(args, 'tags') and args.tags else meta["genre"]) or DEFAULT_TAGS
    rec_date = (args.recording_date if args and hasattr(args, 'recording_date') and args.recording_date else meta["date"])
    thumb_path = (args.thumbnail if args and hasattr(args, 'thumbnail') and args.thumbnail else meta["thumb_path"])

    privacy = (args.privacy if args and hasattr(args, 'privacy') and args.privacy else None) or VIDEO_PRIVACY
    publish_at = args.publish_at if args and hasattr(args, 'publish_at') else None
    license_type = (args.license if args and hasattr(args, 'license') and args.license else None) or "youtube"
    location = args.location if args and hasattr(args, 'location') else None
    default_lang = (args.default_language if args and hasattr(args, 'default_language') and args.default_language else None) or VIDEO_LANGUAGE
    default_audio_lang = (args.default_audio_language if args and hasattr(args, 'default_audio_language') and args.default_audio_language else None) or VIDEO_LANGUAGE
    embeddable = args.embeddable if (args and hasattr(args, 'embeddable') and args.embeddable is not None) else ALLOW_EMBEDDING
    
    target_playlist = PLAYLIST_NAME
    if args and hasattr(args, 'playlist') and args.playlist:
        target_playlist = args.playlist
    elif DYNAMIC_PLAYLISTS and meta["artist"]:
        target_playlist = meta["artist"]

    cred_file = (args.credentials_file if args and hasattr(args, 'credentials_file') and args.credentials_file else None) or CREDENTIALS_FILE
    client_secrets = args.client_secrets if args and hasattr(args, 'client_secrets') else None
    chunksize = args.chunksize if args and hasattr(args, 'chunksize') else 268435456
    open_link = args.open_link if args and hasattr(args, 'open_link') else False

    # Splitting-Prüfung ausführen
    segments = split_video_if_needed(work_path)

    # Fortschritt aus einem evtl. vorherigen fehlgeschlagenen Lauf laden (Segment-Dateiname -> Video-ID)
    progress = load_segment_progress(work_path)
    if progress:
        logging.info(f"Bestehender Fortschritt gefunden: {len(progress)} Segment(e) bereits hochgeladen, werden übersprungen.")

    # Segmente nacheinander hochladen
    for idx, seg in enumerate(segments):
        seg_key = os.path.basename(seg)

        if seg_key in progress:
            logging.info(f"Segment {seg_key} bereits hochgeladen (Video-ID {progress[seg_key]}), überspringe.")
            continue

        part_title = title_base
        if len(segments) > 1:
            part_title = f"{title_base} (Teil {idx + 1}/{len(segments)})"

        try:
            video_id = upload_single_video(
                file_path=seg,
                title=part_title,
                desc=desc_base,
                category=category,
                tags=tags,
                rec_date=rec_date,
                thumb_path=thumb_path,
                playlist_name=target_playlist,
                privacy=privacy,
                publish_at=publish_at,
                license_type=license_type,
                location=location,
                default_lang=default_lang,
                default_audio_lang=default_audio_lang,
                embeddable=embeddable,
                cred_file=cred_file,
                client_secrets_file=client_secrets,
                chunksize=chunksize,
                open_link=open_link
            )
            # Fortschritt sofort persistieren, damit bei einem späteren Fehler nichts verloren geht
            progress[seg_key] = video_id
            save_segment_progress(work_path, progress)
        except Exception as e:
            logging.error(f"Upload-Fehler bei Segment {seg}: {e}")

            if progress:
                # Mind. ein Segment wurde bereits erfolgreich hochgeladen: NICHT nach CORRUPT verschieben,
                # sonst gehen Original + Fortschritt-Zuordnung verloren und bereits hochgeladene Segmente
                # würden bei einem erneuten Lauf ein zweites Mal hochgeladen.
                target_retry = resolve_target_path(RETRY_DIR, filename)
                logging.warning(
                    f"{len(progress)} von {len(segments)} Segment(en) bereits erfolgreich hochgeladen. "
                    f"Verschiebe Original nach RETRY statt CORRUPT, Fortschritt bleibt erhalten: {target_retry}"
                )
                shutil.move(work_path, target_retry)
                # Nur die noch nicht hochgeladenen Segmente lokal aufräumen; bereits hochgeladene
                # Segment-Dateien können ebenfalls entfernt werden, da das Video schon bei YouTube liegt.
                cleanup_work_files(segments, is_error=True)
            else:
                target_corrupt = resolve_target_path(CORRUPT_DIR, filename)
                shutil.move(work_path, target_corrupt)
                cleanup_work_files(segments, is_error=True)
                clear_segment_progress(work_path)
            return

    # Bei Erfolg ins DONE-Verzeichnis verschieben
    target_done = resolve_target_path(DONE_DIR, filename)
    logging.info(f"Verarbeitung erfolgreich. Verschiebe Original nach DONE: {target_done}")
    shutil.move(work_path, target_done)
    cleanup_work_files(segments)
    clear_segment_progress(work_path)


# ==============================================================================
# CLI PARSER & HAUPTEINSTIEGSPUNKT
# ==============================================================================
def parse_arguments():
    """Initialisiert das Parsing der Kommandozeilenargumente."""
    parser = argparse.ArgumentParser(
        description=f"{__title__} v{__version__}",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("file", nargs="?", help="Pfad zur hochzuladenden Videodatei (im manuellen Modus)")
    parser.add_argument("-a", "--auto", action="store_true", help="Automatischer Batch-Modus für ein Verzeichnis")
    parser.add_argument("-D", "--daemon", action="store_true", help="Dämon-Modus: Dauerhafte inotify-Verzeichnisüberwachung")

    parser.add_argument("-t", "--title", help="Video-Titel (Standard: Metadaten/Dateiname)")
    parser.add_argument("-d", "--description", help="Video-Beschreibung")
    parser.add_argument("-c", "--category", help="Kategorie ID oder Name (z.B. Entertainment, Gaming, 22)")
    parser.add_argument("-v", "--version", action="version", version=f"{__title__} v{__version__}")
    parser.add_argument("--tags", help="Kommagetrennte Liste von Tags")
    
    parser.add_argument("--privacy", choices=["public", "private", "unlisted"], default=None, help="Sichtbarkeit")

    parser.add_argument("--thumbnail", help="Pfad zu benutzerdefiniertem Thumbnail-Bild")
    parser.add_argument("--playlist", help="Name der Ziel-Playlist")
    parser.add_argument("--publish-at", help="Geplante Veröffentlichung (ISO-Format 8601)")
    parser.add_argument("--license", choices=["youtube", "creativeCommon"], default=None, help="Videolizenz")
    parser.add_argument("--location", help="Geo-Koordinaten (Format: 'latitude=50.9,longitude=6.9')")
    parser.add_argument("--recording-date", help="Aufnahmedatum")

    parser.add_argument("--default-language", default=None, help="Standardsprache des Titels/der Beschreibung")
    parser.add_argument("--default-audio-language", default=None, help="Standardsprache des Audios")
    parser.add_argument("--embeddable", action="store_true", default=None, help="Einbetten auf externen Seiten erlauben")

    parser.add_argument("--credentials-file", default=None, help="Pfad zur OAuth Credentials JSON")
    parser.add_argument("--client-secrets", help="Pfad zur Google Client Secrets JSON")
    parser.add_argument("--chunksize", type=int, default=268435456, help="Upload Chunk-Größe in Bytes")
    parser.add_argument("--open-link", action="store_true", help="Nach Upload Video-URL im Standardbrowser öffnen")

    return parser.parse_args()


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
        logging.warning("fcntl nicht verfügbar (kein Unix-System) - Lockfile-Schutz übersprungen.")
        return True

    lock_path = os.path.join(tempfile.gettempdir(), "yt-upload.lock")
    try:
        _lock_file_handle = open(lock_path, "w")
        fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file_handle.write(str(os.getpid()))
        _lock_file_handle.flush()
        return True
    except (OSError, BlockingIOError):
        logging.error(
            f"Es läuft bereits eine andere Instanz von {__title__} (Lock: {lock_path}). Breche ab."
        )
        return False


def main():
    """Hauptablaufsteuerung abhängig von den übergebenen Parametern."""
    load_configuration(log_changes=False)
    ensure_directories()

    # Log-Handler initialisieren
    # stdout-Handler: immer aktiv, wird von journald/docker logs erfasst.
    # RotatingFileHandler: nur zusätzlich, wenn ein klassischer Syslog-Daemon (rsyslog,
    # syslog-ng, syslogd) läuft - sonst ist die eigene Logdatei nur eine unnötige zweite
    # Datenhaltung neben dem Journal. Rein stdlib, keine zusätzliche Abhängigkeit.
    log_handlers = [logging.StreamHandler(sys.stdout)]
    syslog_detected = is_syslog_daemon_running()
    file_log_error = None
    if syslog_detected:
        try:
            log_dir = os.path.dirname(LOG_FILE) or '.'
            os.makedirs(log_dir, exist_ok=True)
            log_handlers.append(
                RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            )
        except OSError as e:
            file_log_error = str(e)

    logging.basicConfig(
        level=logging.DEBUG if DEBUG_MODE else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=log_handlers
    )

    if syslog_detected and file_log_error:
        logging.warning(f"Logdatei {LOG_FILE} nicht beschreibbar, verwende nur stdout: {file_log_error}")
    elif syslog_detected:
        logging.info(f"Klassischer Syslog-Daemon erkannt - zusätzliches Datei-Logging nach {LOG_FILE} aktiv.")
    else:
        logging.info("Kein klassischer Syslog-Daemon erkannt - Logging nur nach stdout (journald/docker logs).")

    args = parse_arguments()

    logging.info(f"=== {__title__} v{__version__} gestartet ===")

    if not acquire_instance_lock():
        sys.exit(1)

    # Modus 1: Manueller Upload einer angegebenen Datei
    if args.file:
        if not os.path.exists(args.file):
            logging.error(f"Angegebene Datei existiert nicht: {args.file}")
            sys.exit(1)
        process_single_file(args.file, args)

    # Modus 2: Auto-Batch – verarbeitet alle bereits vorhandenen Dateien nacheinander
    elif args.auto:
        logging.info(f"Starte einmalige Batch-Verarbeitung in {IN_DIR}...")
        while True:
            file_to_process = find_existing_video(IN_DIR)
            if not file_to_process:
                logging.info("Keine weiteren Dateien im Eingangsverzeichnis gefunden.")
                break
            process_single_file(file_to_process, args=None)

    # Modus 3: Dämonen-Modus – Dauerhafte Überwachung mittels Inotify
    elif args.daemon:
        logging.info("Starte Dämon-Modus...")
        
        current_watched_dir = None
        inotify_adapter = None

        try:
            while True:
                load_configuration(log_changes=True)

                # Bei Pfadänderung inotify Tree neu initialisieren
                if HAS_INOTIFY and current_watched_dir != IN_DIR:
                    try:
                        logging.info(f"Initialisiere InotifyTree auf: {IN_DIR}")
                        inotify_adapter = inotify.adapters.InotifyTree(IN_DIR)
                        current_watched_dir = IN_DIR
                    except Exception as e:
                        logging.error(f"Konnte InotifyTree für {IN_DIR} nicht initialisieren: {e}")
                        inotify_adapter = None

                input_file = wait_for_input(IN_DIR, inotify_adapter=inotify_adapter)
                if input_file:
                    process_single_file(input_file, args=None)
        except KeyboardInterrupt:
            logging.info("Dämon-Modus beendet.")

    else:
        print(f"{__title__} v{__version__}\n")
        print("Bitte einen Betriebsmodus wählen:")
        print("  - Einzelne Datei:  python3 yt-upload.py /pfad/zum/video.mp4")
        print("  - Auto-Pipeline:   python3 yt-upload.py -a")
        print("  - Dämon-Modus:     python3 yt-upload.py -D")
        print("\nNutze -h oder --help für alle Optionen.")


if __name__ == "__main__":
    main()

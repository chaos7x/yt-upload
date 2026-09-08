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
Automatischer YouTube Video Uploader & CLI-Uploader v2.0.0 (YouTube Data API v3)
Bietet drei Betriebsmodi:
1. Manuell (CLI): Upload einzelner Dateien wie mit dem klassischen youtube-upload.
2. Auto-Pipeline (-a --auto): Einmalige Batch-Verarbeitung eines Zielverzeichnisses mit FFmpeg-Splitting & Metadatenvererbung.
3. Dämon-Modus (-D --daemon): Dauerhafter Hintergrund-Dienst mit inotify-Überwachung.

SYSTEM-VORAUSSETZUNGEN:
- ffmpeg & ffprobe (im System-PATH vorhanden für Splitting & Thumbnail-Extraktion)
- python3-inotify (optional, aber empfohlen für den -D Dämon-Modus ohne Polling-Overhead)
"""

__title__ = "YouTube Video Uploader & CLI-Uploder"
__version__ = "2.0.3"

import argparse
import configparser
import glob
import hashlib
from importlib import metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import webbrowser
import requests
from datetime import datetime, timezone

if os.environ.get('DEBUG', '').lower() in ('true', 'yes', '1'):
    os.environ['DEBUG'] = '1'
else:
    os.environ['DEBUG'] = '0'

try:
    import inotify.adapters
    HAS_INOTIFY = True
except ImportError:
    inotify = None
    HAS_INOTIFY = False

# ==========================================
# KONFIGURATION & PATHS
# ==========================================
CONF_PATH = os.environ.get('CONFIG_FILE', '/etc/yt-upload/upload.conf')
CONF_D_DIR = os.environ.get('CONF_D_DIR', os.path.join(os.path.dirname(CONF_PATH), 'conf.d'))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Standard-Fallbacks (Docker vs. Bare-Metal Host)
IN_DIR = os.environ.get('IN_DIR', "/videos/in" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "in"))
WORK_DIR = os.environ.get('WORK_DIR', "/videos/work" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "work"))
DONE_DIR = os.environ.get('DONE_DIR', "/videos/done" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "done"))
CORRUPT_DIR = os.environ.get('CORRUPT_DIR', "/videos/corrupt" if os.path.exists("/videos") else os.path.join(BASE_DIR, "videos", "corrupt"))

LOG_FILE = os.environ.get('LOG_FILE', "/log/upload.log" if os.path.exists("/log") else os.path.join(BASE_DIR, "upload.log"))
CREDENTIALS_FILE = os.environ.get('CREDENTIALS_FILE', "/app/oauth/youtube-upload-credentials.json" if os.path.exists("/app/oauth") else os.path.join(BASE_DIR, "youtube-upload-credentials.json"))

SEGMENT_TIME_SEC = 36000  # 10 Stunden Limit (in Sekunden)
CHUNK_UNIT_BYTES = 262144  # YouTube Resumable Upload Chunks müssen Vielfache von 256 KiB sein

DEFAULT_DESCRIPTION = "Automatischer Upload via Script."
DEFAULT_TAGS = "Upload, Video"
DEFAULT_CATEGORY = "Entertainment"

VIDEO_PRIVACY = "unlisted"  # 'public', 'private', 'unlisted'
VIDEO_LANGUAGE = "de"
ALLOW_EMBEDDING = True
PLAYLIST_NAME = ""

AUTO_GENERATE_THUMBNAIL = True
AUTO_THUMB_MIN_SEC = 15
AUTO_THUMB_MAX_SEC = 120
ALLOW_OVERWRITE = True
DYNAMIC_PLAYLISTS = False

# Globale Variable zur Tracking-Vergleichs-Hash-Verwaltung der Configs
CURRENT_CONFIG_HASH = ""

# Globale Variable für die Zensur-Funktionalität der Video-Beschreibung
ENABLE_DESCRIPTION_CENSOR = os.environ.get('ENABLE_DESCRIPTION_CENSOR', 'false').lower() in ('true', 'yes', '1')
_env_blacklist = os.environ.get('DESCRIPTION_BLACKLIST', '')
DESCRIPTION_BLACKLIST = [w.strip() for w in _env_blacklist.split(',') if w.strip()]


def load_configuration(log_changes=False):
    """
    Liest upload.conf und alle conf.d/*.conf Dateien dynamisch ein.
    Erkennt über MD5-Hash-Vergleiche Dateiänderungen im laufenden Dämon-Betrieb.
    """
    global IN_DIR, WORK_DIR, DONE_DIR, CORRUPT_DIR, LOG_FILE, CREDENTIALS_FILE
    global DEFAULT_DESCRIPTION, DEFAULT_TAGS, DEFAULT_CATEGORY, VIDEO_PRIVACY
    global VIDEO_LANGUAGE, ALLOW_EMBEDDING, PLAYLIST_NAME, AUTO_GENERATE_THUMBNAIL
    global AUTO_THUMB_MIN_SEC, AUTO_THUMB_MAX_SEC, ALLOW_OVERWRITE, DYNAMIC_PLAYLISTS
    global CURRENT_CONFIG_HASH
    global ENABLE_DESCRIPTION_CENSOR, DESCRIPTION_BLACKLIST

    config = configparser.ConfigParser()
    config_files = []

    if os.path.isfile(CONF_PATH):
        config_files.append(CONF_PATH)

    if os.path.isdir(CONF_D_DIR):
        config_files.extend(sorted(glob.glob(os.path.join(CONF_D_DIR, "*.conf"))))

    # Hash über Inhalte und Modifikationsdaten aller Config-Dateien bilden
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

    new_hash = hasher.hexdigest()

    # Dynamic Reload Logging
    if log_changes and CURRENT_CONFIG_HASH and new_hash != CURRENT_CONFIG_HASH:
        files_str = ", ".join(file_list_names) if file_list_names else "conf.d"
        logging.info(f"🔄 Konfigurationsänderung erkannt (geändert: {files_str}). Synchronisiere...")

    CURRENT_CONFIG_HASH = new_hash

    if config_files:
        config.read(config_files, encoding='utf-8')

        # 2. Config-Datei(en) einlesen (upload.conf + conf.d/*.conf)
        if 'paths' in config:
            IN_DIR = config.get('paths', 'in_dir', fallback=IN_DIR)
            WORK_DIR = config.get('paths', 'work_dir', fallback=WORK_DIR)
            DONE_DIR = config.get('paths', 'done_dir', fallback=DONE_DIR)
            CORRUPT_DIR = config.get('paths', 'corrupt_dir', fallback=CORRUPT_DIR)
            LOG_FILE = config.get('paths', 'log_file', fallback=LOG_FILE)
            CREDENTIALS_FILE = config.get('paths', 'credentials_file', fallback=CREDENTIALS_FILE)

        if 'settings' in config:
            DEFAULT_DESCRIPTION = config.get('settings', 'default_description', fallback=DEFAULT_DESCRIPTION)
            DEFAULT_TAGS = config.get('settings', 'default_tags', fallback=DEFAULT_TAGS)
            DEFAULT_CATEGORY = config.get('settings', 'default_category', fallback=DEFAULT_CATEGORY)
            VIDEO_PRIVACY = config.get('settings', 'privacy_status', fallback=VIDEO_PRIVACY)
            VIDEO_LANGUAGE = config.get('settings', 'default_language', fallback=VIDEO_LANGUAGE)
            ALLOW_EMBEDDING = config.getboolean('settings', 'allow_embedding', fallback=ALLOW_EMBEDDING)
            PLAYLIST_NAME = config.get('settings', 'playlist_name', fallback=PLAYLIST_NAME)
            AUTO_GENERATE_THUMBNAIL = config.getboolean('settings', 'auto_generate_thumbnail', fallback=AUTO_GENERATE_THUMBNAIL)
            AUTO_THUMB_MIN_SEC = config.getint('settings', 'auto_thumb_min_sec', fallback=AUTO_THUMB_MIN_SEC)
            AUTO_THUMB_MAX_SEC = config.getint('settings', 'auto_thumb_max_sec', fallback=AUTO_THUMB_MAX_SEC)
            ALLOW_OVERWRITE = config.getboolean('settings', 'allow_overwrite', fallback=ALLOW_OVERWRITE)
            ENABLE_DESCRIPTION_CENSOR = config.getboolean('settings', 'enable_description_censor', fallback=ENABLE_DESCRIPTION_CENSOR)

        if 'blacklist' in config:
            # Lese Schlüssel aus der INI
            ini_blacklist = [key.strip() for key in config.options('blacklist') if key.strip() != '__name__']
            # Falls INI-Einträge existieren, nutze diese, ansonsten behalte den Wert aus der .env
            if ini_blacklist:
                DESCRIPTION_BLACKLIST = ini_blacklist

    # 3. Dynamic Playlists (Env Var überschreibt Config, falls gesetzt)
    DYNAMIC_PLAYLISTS = os.getenv(
        "ENABLE_DYNAMIC_PLAYLISTS",
        str(config.getboolean('settings', 'enable_dynamic_playlists', fallback=False) if config_files else "false")
    ).lower() in ("1", "true", "yes")


# Erstmaliges Laden beim Modul-Import/Start
load_configuration(log_changes=False)


# ==========================================
# OAUTH TOKEN HELPER
# ==========================================
def get_access_token(cred_file=None, client_secrets_file=None):
    """Liest den Refresh Token aus der JSON-Datei und holt ein frisches Access Token via Google OAuth API."""
    target_cred = cred_file or CREDENTIALS_FILE
    if not os.path.exists(target_cred):
        raise FileNotFoundError(f"Credentials-Datei nicht gefunden: {target_cred}")

    with open(target_cred, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Unterstützung für verschiedene OAuth-JSON-Strukturen (Google Client Secrets vs Token Files)
    client_data = data
    if client_secrets_file:
        with open(client_secrets_file, "r", encoding="utf-8") as f:
            client_data = json.load(f)

    client_id = client_data.get("client_id") or client_data.get("installed", {}).get("client_id") or client_data.get("web", {}).get("client_id")
    client_secret = client_data.get("client_secret") or client_data.get("installed", {}).get("client_secret") or client_data.get("web", {}).get("client_secret")
    refresh_token = data.get("refresh_token")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(f"OAuth-Credentials in {target_cred} unvollständig (client_id, client_secret oder refresh_token fehlt).")

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


# ==========================================
# HELPER & CLEANUP
# ==========================================
def ensure_directories():
    for d in [IN_DIR, WORK_DIR, DONE_DIR, CORRUPT_DIR]:
        try:
            os.makedirs(d, exist_ok=True)
        except (PermissionError, OSError) as e:
            sys.stderr.write(f"Warnung: Kann Verzeichnis {d} nicht anlegen ({e}).\n")


def unique_path(directory, filename):
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
    candidate = os.path.join(directory, filename)
    if ALLOW_OVERWRITE:
        return candidate
    return unique_path(directory, filename)


def cleanup_work_files(work_paths, is_error=False):
    log_func = logging.warning if is_error else logging.info
    log_func("Bereinige Dateien des aktuellen Jobs im WORK-Verzeichnis...")
    for item_path in work_paths:
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.unlink(item_path)
        except OSError as e:
            logging.error(f"Fehler beim Löschen von {item_path}: {e}")


def cleanup_generated_thumbnail(thumb_path):
    if not thumb_path:
        return

    temp_dir = os.path.abspath(tempfile.gettempdir())
    candidate = os.path.abspath(thumb_path)
    if os.path.dirname(candidate) != temp_dir or not os.path.basename(candidate).startswith("yt_thumb_"):
        return

    try:
        if os.path.isfile(candidate):
            os.unlink(candidate)
    except OSError as e:
        logging.warning(f"Generiertes Thumbnail kann nicht gelöscht werden ({candidate}): {e}")


def is_file_ready_and_valid(file_path, wait_interval=3, max_checks=10):
    """
    Prüft, ob eine Datei vollständig geschrieben wurde (z.B. bei laufendem Kopiervorgang)
    und ob der Container (moov-Atom) valide ist.
    """
    if not os.path.exists(file_path):
        return False

    # 1. Prüfen, ob die Dateigröße sich noch ändert (Kopiervorgang im Gange)
    last_size = -1
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
            # Größe stabil
            break
        elif last_size != -1:
            logging.info(f"Datei wächst noch ({current_size / (1024*1024):.1f} MB)... warte weiter.")

        last_size = current_size
        time.sleep(wait_interval)
    else:
        # Größe hat sich kontinuierlich geändert oder war 0
        logging.warning(f"Datei wird nach {max_checks * wait_interval}s noch geschrieben: {os.path.basename(file_path)}")
        return False

    # 2. FFprobe-Integritätscheck
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


# ==========================================
# INOTIFY & INPUT FINDER
# ==========================================
def find_existing_video(target_dir=IN_DIR):
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")
    if not os.path.isdir(target_dir):
        return None
    for root, _, files in os.walk(target_dir):
        for file in files:
            if file.lower().endswith(valid_exts):
                return os.path.join(root, file)
    return None


def wait_for_input(target_dir=IN_DIR):
    # Prüfe vor der Suche auf geänderte Config-Dateien
    load_configuration(log_changes=True)

    existing = find_existing_video(target_dir)
    if existing:
        logging.info(f"Bestehende Datei gefunden: {existing}")
        return existing

    logging.info(f"Warte via inotify auf neue Dateien in {target_dir}...")
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")

    if not HAS_INOTIFY:
        logging.warning("inotify-Modul nicht verfügbar, nutze Polling-Fallback.")
        while True:
            time.sleep(10)
            load_configuration(log_changes=True)
            existing = find_existing_video(target_dir)
            if existing:
                return existing

    i = inotify.adapters.InotifyTree(target_dir) # type: ignore

    while True:
        try:
            for event in i.event_gen(yield_nones=False, timeout_s=10):
                # Prüfe bei jedem Timeout/Event die Konfigurations-Hashes
                load_configuration(log_changes=True)

                if event is None:
                    existing = find_existing_video(target_dir)
                    if existing:
                        logging.info(f"Datei via Fallback-Timer erkannt: {existing}")
                        return existing
                    continue

                (_, type_names, path, filename) = event
                if any(t in type_names for t in ["IN_CLOSE_WRITE", "IN_MOVED_TO"]):
                    if filename.lower().endswith(valid_exts):
                        full_path = os.path.join(path, filename)
                        logging.info(f"Datei erfolgreich via inotify erkannt: {full_path}")
                        return full_path
        except Exception as e:
            logging.error(f"Fehler beim Inotify-Observer: {e}")
            time.sleep(5)
            existing = find_existing_video(target_dir)
            if existing:
                return existing


# ==========================================
# METADATEN & THUMBNAIL
# ==========================================
def truncate_title(title: str, max_length: int = 100) -> str:
    if not title:
        return ""
    if len(title) <= max_length:
        return title
    return title[: max_length - 3].rstrip() + "..."


def get_valid_category_id(category_input):
    """Mappt Textgenres oder Namen auf offizielle YouTube Category IDs."""
    if not category_input:
        return "22"  # Standard: People & Blogs

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
    if not text:
        return text
    normalized = unicodedata.normalize('NFKD', text)
    cleaned_chars = [c for c in normalized if unicodedata.category(c) not in ('Mn', 'So')]
    result = unicodedata.normalize('NFC', ''.join(cleaned_chars))
    return re.sub(r'\s+', ' ', result).strip()


def parse_location(location_str):
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
        # Wandelt alle Tag-Schlüssel zuverlässig in Kleinschreibung um
        tags = {str(k).lower(): v for k, v in format_info.get("tags", {}).items()}

        if "duration" in format_info:
            metadata["duration"] = int(float(format_info["duration"]))

        # 'title' statt 'TITLE' abfragen
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

    # Eindeutige temporäre Pfade nutzen
    temp_dir = tempfile.gettempdir()
    pid = os.getpid()
    timestamp = int(time.time() * 1000)
    thumb_path = os.path.join(temp_dir, f"yt_thumb_{pid}_{timestamp}.jpg")
    temp_attach = os.path.join(temp_dir, f"yt_attach_{pid}_{timestamp}.jpg")

    try:
        # 1. Versuch: In der Datei eingebettetes Cover/Attachment extrahieren (z.B. MKV/MP4)
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

        # 2. Versuch: Automatische Frame-Extraktion (falls kein Cover vorhanden & auto_generate_thumbnail = True)
        if (not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0) and AUTO_GENERATE_THUMBNAIL:
            duration = metadata["duration"]
            
            # Ziel-Timestamp ermitteln
            if duration <= 0:
                seek_sec = AUTO_THUMB_MIN_SEC
            elif duration < AUTO_THUMB_MIN_SEC:
                seek_sec = max(1, duration // 2)  # Bei sehr kurzen Videos genau die Mitte wählen
            else:
                # Wähle die Mitte zwischen Min und Max, begrenze aber durch die tatsächliche Videolänge
                target_sec = AUTO_THUMB_MIN_SEC + ((AUTO_THUMB_MAX_SEC - AUTO_THUMB_MIN_SEC) // 2)
                seek_sec = min(target_sec, max(1, duration - 1))

            logging.info(f"Generiere Auto-Thumbnail bei Sekunde {seek_sec} (Video-Dauer: {duration}s)...")

            # Frame extrahieren
            cmd_frame = [
                "ffmpeg", "-y",
                "-ss", str(seek_sec),
                "-i", file_path,
                "-frames:v", "1",
                "-q:v", "2",
                thumb_path
            ]
            subprocess.run(cmd_frame, capture_output=True, text=True)

            # Fallback: Falls der Seek fehlgeschlagen ist (z.B. unvollständige Keyframe-Indexe), nimm den ersten I-Frame
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
        if os.path.exists(temp_attach):
            try:
                os.remove(temp_attach)
            except OSError:
                pass

    return metadata


# ==========================================
# BESCHREIBUNGS-ZENSUR
# ==========================================
def censor_text(text: str) -> str:
    """
    Zensiert sensible Begriffe in Texten (Titel oder Beschreibung)
    anhand der Konfigurations-Blacklist.
    """
    if not ENABLE_DESCRIPTION_CENSOR or not text:
        return text

    if DESCRIPTION_BLACKLIST:
        escaped_words = [re.escape(word) for word in DESCRIPTION_BLACKLIST if word.strip()]
        if escaped_words:
            # \b funktioniert bei Domains oft nicht perfekt vor Punkten,
            # daher nutzen wir hier einen flexibleren Ansatz für Wörter und Domains
            pattern = re.compile(r'(?i)' + '|'.join(escaped_words))

            def replace_match(match):
                matched_str = match.group(0)
                # Entweder kompletter Sternchen-Ersatz oder ein fixer Platzhalter
                return '*' * len(matched_str)

            text = pattern.sub(replace_match, text)

    return text

# ==========================================
# FFMPEG LOSSLESS SPLITTER
# ==========================================
def split_video_if_needed(work_path):
    """
    Prüft die Videodauer. Ist sie größer als SEGMENT_TIME_SEC, wird die Datei
    verlustfrei in Segmente aufgeteilt.
    WICHTIG: Die Originaldatei work_path wird hier NICHT gelöscht, damit sie
    anschließend in DONE_DIR archiviert werden kann.
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

    # Alte Segmente anhand von Prefix und Extension sauber bereinigen
    for item in os.listdir(WORK_DIR):
        if item.startswith(f"{base_name}_part") and item.endswith(ext):
            stale_segment = os.path.join(WORK_DIR, item)
            try:
                os.remove(stale_segment)
            except OSError as e:
                raise RuntimeError(f"Altes Segment kann nicht gelöscht werden: {stale_segment}") from e

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


# ==========================================
# NATIVE REST API UPLOADER & PLAYLIST HELPER
# ==========================================
def add_video_to_playlist(video_id, playlist_name, access_token, privacy=VIDEO_PRIVACY):
    if not playlist_name or not video_id:
        return False

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        playlist_id = None
        next_page = None

        # Paginierung bei mehr als 50 Playlists
        while True:
            list_url = f"https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50"
            if next_page:
                list_url += f"&pageToken={next_page}"

            res = requests.get(list_url, headers=headers, timeout=30)
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

        if not playlist_id:
            create_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet,status"
            create_body = {
                "snippet": {"title": playlist_name, "description": "Automatisch erstellt"},
                "status": {"privacyStatus": privacy}
            }
            create_res = requests.post(create_url, headers=headers, json=create_body, timeout=30)
            if create_res.status_code in (200, 201):
                playlist_id = create_res.json().get("id")

        if playlist_id:
            item_url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
            item_body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id}
                }
            }
            item_res = requests.post(item_url, headers=headers, json=item_body, timeout=30)
            if item_res.status_code in (200, 201):
                logging.info(f"Video {video_id} erfolgreich zur Playlist '{playlist_name}' hinzugefügt.")
                return True
            else:
                logging.warning(f"Video konnte Playlist nicht hinzugefügt werden: {item_res.text}")

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
    # YouTube API Limits einhalten (Titel max 100, Beschreibung max 5000 Zeichen)
    if title:
        title = truncate_title(sanitize_text(title), max_length=100)
    else:
        # Fallback auf Dateinamen, falls gar kein Titel übergeben wurde
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        title = truncate_title(sanitize_text(base_name), max_length=100)

    if desc:
        desc = desc[:5000]

    # Wenn ein Veröffentlichungszeitpunkt gesetzt ist, muss der Status 'private' sein
    if publish_at and privacy != "private":
        logging.info(f"Status wurde für geplanten Upload von '{privacy}' auf 'private' korrigiert.")
        privacy = "private"

    logging.info(f"Lade hoch via native HTTP REST API ({privacy}): {os.path.basename(file_path)}")

    file_size = os.path.getsize(file_path)
    access_token = get_access_token(cred_file, client_secrets_file)

    # Session für Verbindungs-Wiederverwendung (Performance)
    session = requests.Session()

    # Chunksize muss ein Vielfaches von 256 KiB sein
    if chunksize % CHUNK_UNIT_BYTES != 0:
        adjusted_chunksize = max(CHUNK_UNIT_BYTES, (chunksize // CHUNK_UNIT_BYTES) * CHUNK_UNIT_BYTES)
        logging.info(f"Chunksize angepasst auf Vielfaches von 256 KiB: {chunksize} -> {adjusted_chunksize} Bytes")
        chunksize = adjusted_chunksize

# Safe Tags Sanitation & Limits (max 100 Zeichen pro Tag, max 400 Zeichen gesamt)
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

    # Safe Category fallback
    cat_id = get_valid_category_id(category) if 'get_valid_category_id' in globals() else category
    if not cat_id:
        cat_id = "22"  # Standard YouTube Kategorie: People & Blogs

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

    # DEBUG LOGGING: Gibt das exakte JSON-Payload im Log aus
    logging.info(f"PAYLOAD DEBUG: {json.dumps(metadata_body, ensure_ascii=False)}")

    parts = "snippet,status"
    if "recordingDetails" in metadata_body:
        parts += ",recordingDetails"

    # Initialisiere die Resumable Upload Session (EINMAL vor der Upload-Schleife)
    init_url = f"https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part={parts}"
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/*"
    }

    logging.info("Initialisiere Resumable Upload Session...")

    init_res = session.post(init_url, headers=init_headers, json=metadata_body, timeout=60)
    if init_res.status_code != 200:
        raise RuntimeError(f"Session-Init fehlgeschlagen ({init_res.status_code}): {init_res.text}")

    upload_url = init_res.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Keine Upload-Location im Header erhalten.")

    logging.info(f"Starte Chunk-Upload ({file_size / (1024 * 1024):.2f} MB) mit Chunksize {chunksize / (1024 * 1024):.1f} MB...")

    max_retries = 5
    video_id = None
    uploaded_bytes = 0

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

            # Retry-Schleife für den jeweiligen Chunk
            chunk_success = False
            for attempt in range(1, max_retries + 1):
                try:

                    put_res = session.put(upload_url, headers=chunk_headers, data=chunk, timeout=120)

                    if put_res.status_code in (200, 201):
                        resp_data = put_res.json()
                        video_id = resp_data.get("id")
                        uploaded_bytes = file_size
                        chunk_success = True
                        logging.info(f"Upload ERFOLGREICH abgeschlossen! Video-ID: {video_id}")
                        break

                    elif put_res.status_code == 308:
                        # Upload unvollständig, Range-Header prüfen
                        range_hdr = put_res.headers.get("Range")
                        if range_hdr and "-" in range_hdr:
                            uploaded_bytes = int(range_hdr.split("-")[1]) + 1
                        else:
                            uploaded_bytes += chunk_len

                        # Sicherheits-Check: Setze den Offset auf das nächste Vielfache von 256 KiB zurück
                        if uploaded_bytes < file_size and uploaded_bytes % CHUNK_UNIT_BYTES != 0:
                            uploaded_bytes = (uploaded_bytes // CHUNK_UNIT_BYTES) * CHUNK_UNIT_BYTES
                            logging.warning(f"Offset korrigiert auf 256-KiB-Grenze: {uploaded_bytes} Bytes")

                        pct = (uploaded_bytes / file_size) * 100
                        logging.info(f"Fortschritt: {uploaded_bytes / (1024*1024):.1f} / {file_size / (1024*1024):.1f} MB ({pct:.1f}%)")
                        chunk_success = True
                        break

                    elif put_res.status_code in (401, 403):
                        access_token = get_access_token(cred_file, client_secrets_file)
                        chunk_headers["Authorization"] = f"Bearer {access_token}"
                        raise RuntimeError("Token erneuert, versuche Chunk erneut...")

                except Exception as e:
                    logging.warning(f"Fehler bei Chunk-Upload (Versuch {attempt}/{max_retries}): {e}")
                    time.sleep(2 ** attempt)

            if not chunk_success:
                raise RuntimeError("Max Retries beim Upload überschritten.")

    try:
        if video_id:
            if thumb_path and os.path.exists(thumb_path):
                try:
                    logging.info(f"Lade benutzerdefiniertes Thumbnail hoch: {thumb_path}")
                    thumb_url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
                    with open(thumb_path, "rb") as tf:
                        t_res = session.post(
                            thumb_url,
                            headers={
                                "Authorization": f"Bearer {access_token}",
                                "Content-Type": "image/jpeg"
                            },
                            data=tf,
                            timeout=60
                        )
                    if t_res.status_code in (200, 201):
                        logging.info("Thumbnail erfolgreich gesetzt.")
                    else:
                        logging.warning(f"Thumbnail-Upload fehlgeschlagen ({t_res.status_code}): {t_res.text}")
                except Exception as te:
                    logging.warning(f"Fehler beim Thumbnail-Setzen: {te}")

            if playlist_name:
                add_video_to_playlist(video_id, playlist_name, access_token, privacy)

            if open_link:
                v_url = f"https://www.youtube.com/watch?v={video_id}"
                logging.info(f"Öffne Browser-Link: {v_url}")
                webbrowser.open(v_url)
    finally:
        cleanup_generated_thumbnail(thumb_path)

    return video_id


# ==========================================
# BATCH PIPELINE LOGIK
# ==========================================
def process_single_file(file_path, args=None):
    filename = os.path.basename(file_path)
    logging.info(f"--- VERARBEITE DATEI: {filename} ---")

    if not is_file_ready_and_valid(file_path):
        logging.error(f"Datei unvollständig oder ungültig: {filename}. Verschiebe nach CORRUPT...")
        target_corrupt = resolve_target_path(CORRUPT_DIR, filename)
        shutil.move(file_path, target_corrupt)
        return

    # In WORK-Verzeichnis verschieben
    work_path = resolve_target_path(WORK_DIR, filename)
    logging.info(f"Verschiebe nach WORK: {work_path}")
    shutil.move(file_path, work_path)

    # Metadaten & Thumbnail auslesen
    meta = extract_metadata_and_thumb(work_path)

    # für die Zensur vorbereiten
    raw_title = (args.title if args and args.title else meta["title"]) or os.path.splitext(filename)[0]
    raw_desc = (args.description if args and args.description else meta["description"]) or DEFAULT_DESCRIPTION

    # CLI-Overrides oder Fallbacks
    title_base = censor_text(raw_title)
    desc_base = censor_text(raw_desc) # Zensur anwenden
    category = (args.category if args and args.category else meta["genre"]) or DEFAULT_CATEGORY
    tags = (args.tags if args and args.tags else meta["genre"]) or DEFAULT_TAGS
    rec_date = (args.recording_date if args and args.recording_date else meta["date"])
    thumb_path = (args.thumbnail if args and args.thumbnail else meta["thumb_path"])

    privacy = args.privacy if args and args.privacy else VIDEO_PRIVACY
    publish_at = args.publish_at if args else None
    license_type = args.license if args and args.license else "youtube"
    location = args.location if args else None
    default_lang = args.default_language if args and args.default_language else VIDEO_LANGUAGE
    default_audio_lang = args.default_audio_language if args and args.default_audio_language else VIDEO_LANGUAGE
    embeddable = args.embeddable if args and args.embeddable is not None else ALLOW_EMBEDDING

    # Dynamisches oder festes Playlist-Mapping
    target_playlist = PLAYLIST_NAME
    if args and args.playlist:
        target_playlist = args.playlist
    elif DYNAMIC_PLAYLISTS and meta["artist"]:
        target_playlist = meta["artist"]

    cred_file = args.credentials_file if args else CREDENTIALS_FILE
    client_secrets = args.client_secrets if args else None
    chunksize = args.chunksize if args else 268435456
    open_link = args.open_link if args else False

    # Splitting-Check
    segments = split_video_if_needed(work_path)

    # Segmentweise hochladen
    for idx, seg in enumerate(segments):
        part_title = title_base
        if len(segments) > 1:
            part_title = f"{title_base} (Teil {idx + 1}/{len(segments)})"

        try:
            upload_single_video(
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
        except Exception as e:
            logging.error(f"Upload-Fehler bei Segment {seg}: {e}")
            target_corrupt = resolve_target_path(CORRUPT_DIR, filename)
            shutil.move(work_path, target_corrupt)
            cleanup_work_files(segments, is_error=True)
            return

    # Erfolgreich verarbeitet: In DONE-Verzeichnis verschieben
    target_done = resolve_target_path(DONE_DIR, filename)
    logging.info(f"Verarbeitung erfolgreich. Verschiebe Original nach DONE: {target_done}")
    shutil.move(work_path, target_done)
    cleanup_work_files(segments)


# ==========================================
# CLI PARSER & MAIN ENTRYPOINT
# ==========================================
def parse_arguments():
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
    parser.add_argument("-V", "--Version", action="version", version=f"{__title__} v{__version__}")
    parser.add_argument("--tags", help="Kommagetrennte Liste von Tags")
    parser.add_argument("--privacy", choices=["public", "private", "unlisted"], default=VIDEO_PRIVACY, help="Sichtbarkeit")

    parser.add_argument("--thumbnail", help="Pfad zu benutzerdefiniertem Thumbnail-Bild")
    parser.add_argument("--playlist", help="Name der Ziel-Playlist")
    parser.add_argument("--publish-at", help="Geplante Veröffentlichung (ISO-Format 8601: YYYY-MM-DDTHH:MM:SS.sZ)")
    parser.add_argument("--license", choices=["youtube", "creativeCommon"], default="youtube", help="Videolizenz")
    parser.add_argument("--location", help="Geo-Koordinaten (Format: 'latitude=50.9,longitude=6.9')")
    parser.add_argument("--recording-date", help="Aufnahmedatum (ISO-Format: YYYY-MM-DDTHH:MM:SS.sZ)")

    parser.add_argument("--default-language", default=VIDEO_LANGUAGE, help="Standardsprache des Titels/der Beschreibung")
    parser.add_argument("--default-audio-language", default=VIDEO_LANGUAGE, help="Standardsprache des Audios")
    parser.add_argument("--embeddable", action="store_true", default=ALLOW_EMBEDDING, help="Einbetten auf externen Seiten erlauben")

    parser.add_argument("--credentials-file", default=CREDENTIALS_FILE, help="Pfad zur OAuth Credentials JSON")
    parser.add_argument("--client-secrets", help="Pfad zur Google Client Secrets JSON")
    parser.add_argument("--chunksize", type=int, default=268435456, help="Upload Chunk-Größe in Bytes (Standard: 256 MB)")
    parser.add_argument("--open-link", action="store_true", help="Nach Upload Video-URL im Standardbrowser öffnen")

    return parser.parse_args()


def main():
    ensure_directories()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8") if os.access(os.path.dirname(LOG_FILE) or '.', os.W_OK) else logging.NullHandler()
        ]
    )

    args = parse_arguments()

    logging.info(f"=== {__title__} v{__version__} gestartet ===")

    # 1. Manuelle CLI-Ausführung einer einzelnen Datei
    if args.file:
        if not os.path.exists(args.file):
            logging.error(f"Angegebene Datei existiert nicht: {args.file}")
            sys.exit(1)
        process_single_file(args.file, args)

    # 2. Einmaliger Batch-Modus
    elif args.auto:
        logging.info(f"Starte einmalige Batch-Verarbeitung in {IN_DIR}...")
        while True:
            file_to_process = find_existing_video(IN_DIR)
            if not file_to_process:
                logging.info("Keine weiteren Dateien im Eingangsverzeichnis gefunden.")
                break
            process_single_file(file_to_process, args)

    # 3. Dauerhafter Dämon-Modus
    elif args.daemon:
        logging.info(f"Starte Dämon-Modus mit inotify-Überwachung auf {IN_DIR}...")
        try:
            while True:
                input_file = wait_for_input(IN_DIR)
                if input_file:
                    process_single_file(input_file, args)
        except KeyboardInterrupt:
            logging.info("Dämon durch Benutzer gestoppt (SIGINT). Auf Wiedersehen!")

    else:
        # Fallback: Kein Parameter -> Zeige Hilfetext
        print(f"{__title__} v{__version__}\n")
        print("Bitte einen Betriebsmodus wählen:")
        print("  - Einzelne Datei:  python3 yt-upload.py /pfad/zum/video.mp4")
        print("  - Auto-Pipeline:   python3 yt-upload.py -a")
        print("  - Dämon-Modus:     python3 yt-upload.py -D")
        print("\nNutze -h oder --help für alle Optionen.")


if __name__ == "__main__":
    main()

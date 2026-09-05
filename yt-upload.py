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
__version__ = "2.0.0"

import argparse
import configparser
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

# 2. Config-Datei einlesen (falls vorhanden)
config = configparser.ConfigParser()
if os.path.isfile(CONF_PATH):
    config.read(CONF_PATH)

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

# 3. Dynamic Playlists (Env Var überschreibt Config, falls gesetzt)
DYNAMIC_PLAYLISTS = os.getenv(
    "ENABLE_DYNAMIC_PLAYLISTS",
    str(config.getboolean('settings', 'enable_dynamic_playlists', fallback=False) if os.path.isfile(CONF_PATH) else "false")
).lower() in ("1", "true", "yes")


# ==========================================
# OAUTH TOKEN HELPER
# ==========================================
def get_access_token(cred_file=None):
    """Liest den Refresh Token aus der JSON-Datei und holt ein frisches Access Token via Google OAuth API."""
    target_cred = cred_file or CREDENTIALS_FILE
    if not os.path.exists(target_cred):
        raise FileNotFoundError(f"Credentials-Datei nicht gefunden: {target_cred}")

    with open(target_cred, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Unterstützung für verschiedene OAuth-JSON-Strukturen (Google Client Secrets vs Token Files)
    client_id = data.get("client_id") or data.get("installed", {}).get("client_id") or data.get("web", {}).get("client_id")
    client_secret = data.get("client_secret") or data.get("installed", {}).get("client_secret") or data.get("web", {}).get("client_secret")
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


def cleanup_work_dir():
    logging.warning("Bereinige WORK-Verzeichnis...")
    if os.path.exists(WORK_DIR):
        for item in os.listdir(WORK_DIR):
            item_path = os.path.join(WORK_DIR, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                logging.error(f"Fehler beim Löschen von {item_path}: {e}")


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
            existing = find_existing_video(target_dir)
            if existing:
                return existing

    i = inotify.adapters.InotifyTree(target_dir)

    while True:
        try:
            for event in i.event_gen(yield_nones=False, timeout_s=60):
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
        tags = {k.upper(): v for k, v in format_info.get("tags", {}).items()}

        if "duration" in format_info:
            metadata["duration"] = int(float(format_info["duration"]))

        metadata["title"] = sanitize_text(tags.get("TITLE"))
        metadata["description"] = tags.get("DESCRIPTION") or tags.get("COMMENT")
        metadata["purl"] = tags.get("PURL")
        metadata["genre"] = tags.get("GENRE")
        metadata["date"] = tags.get("DATE")
        metadata["artist"] = sanitize_text(tags.get("ARTIST") or tags.get("ALBUM_ARTIST"))

    except Exception as e:
        logging.error(f"Fehler beim Auslesen der Metadaten via FFprobe: {e}")

    # Eindeutige temporäre Pfade nutzen
    temp_dir = tempfile.gettempdir()
    pid = os.getpid()
    timestamp = int(time.time() * 1000)
    thumb_path = os.path.join(temp_dir, f"yt_thumb_{pid}_{timestamp}.jpg")
    temp_attach = os.path.join(temp_dir, f"yt_attach_{pid}_{timestamp}")

    try:
        # 1. Versuch: In der Datei eingebettetes Cover/Attachment extrahieren (z.B. MKV/MP4)
        cmd_mkv = ["ffmpeg", "-y", "-dump_attachment:t:0", temp_attach, "-i", file_path]
        subprocess.run(cmd_mkv, capture_output=True, text=True)

        if not os.path.exists(temp_attach) or os.path.getsize(temp_attach) == 0:
            cmd_mp4 = ["ffmpeg", "-y", "-i", file_path, "-map", "0:v", "-map", "-0:V", "-c", "copy", temp_attach]
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
    part_idx = 1
    while True:
        seg_candidate = os.path.join(WORK_DIR, f"{base_name}_part{part_idx:02d}{ext}")
        if os.path.exists(seg_candidate):
            created_segments.append(seg_candidate)
            part_idx += 1
        else:
            break

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
    chunksize=104857600,
    open_link=False
):
    logging.info(f"Lade hoch via native HTTP REST API ({privacy}): {os.path.basename(file_path)}")

    file_size = os.path.getsize(file_path)
    access_token = get_access_token(cred_file)

    # Chunksize muss ein Vielfaches von 256 KiB sein
    if chunksize % CHUNK_UNIT_BYTES != 0:
        adjusted_chunksize = max(CHUNK_UNIT_BYTES, (chunksize // CHUNK_UNIT_BYTES) * CHUNK_UNIT_BYTES)
        logging.info(f"Chunksize angepasst auf Vielfaches von 256 KiB: {chunksize} -> {adjusted_chunksize} Bytes")
        chunksize = adjusted_chunksize

    tags_list = []
    if isinstance(tags, list):
        tags_list = tags
    elif isinstance(tags, str) and tags:
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]

    snippet = {
        "title": title,
        "description": desc or "",
        "categoryId": get_valid_category_id(category),
        "tags": tags_list,
    }
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
        metadata_body["recordingDetails"] = {"recordingDate": rec_date}

    parsed_loc = parse_location(location) if isinstance(location, str) else location
    if parsed_loc:
        metadata_body["recordingDetails"] = metadata_body.get("recordingDetails", {})
        metadata_body["recordingDetails"]["location"] = parsed_loc

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
    init_res = requests.post(init_url, headers=init_headers, json=metadata_body, timeout=60)
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
                    put_res = requests.put(upload_url, headers=chunk_headers, data=chunk, timeout=120)

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

                        # Sicherheits-Check: Setze den Offset auf das nächste Vielfache von 256 KiB zurück, 
                        # falls Google den Stream an einer ungeraden Byte-Grenze unterbrochen hat.
                        if uploaded_bytes < file_size and uploaded_bytes % CHUNK_UNIT_BYTES != 0:
                            uploaded_bytes = (uploaded_bytes // CHUNK_UNIT_BYTES) * CHUNK_UNIT_BYTES
                            logging.warning(f"Offset korrigiert auf 256-KiB-Grenze: {uploaded_bytes} Bytes")

                        pct = (uploaded_bytes / file_size) * 100
                        logging.info(f"Fortschritt: {uploaded_bytes / (1024*1024):.1f} / {file_size / (1024*1024):.1f} MB ({pct:.1f}%)")
                        chunk_success = True
                        break

                    else:
                        raise RuntimeError(f"HTTP-Fehler beim Chunk-Upload ({put_res.status_code}): {put_res.text}")

                except Exception as e:
                    logging.warning(f"Chunk-Upload Versuch {attempt}/{max_retries} fehlgeschlagen: {e}")
                    if attempt == max_retries:
                        raise

                    time.sleep(attempt * 5)

                    # Token erneuern
                    try:
                        access_token = get_access_token(cred_file)
                    except Exception as tok_err:
                        logging.warning(f"Konnte Token nicht auffrischen: {tok_err}")

                    # Google Upload-Status abfragen (Resumable Status Check)
                    try:
                        status_headers = {
                            "Authorization": f"Bearer {access_token}",
                            "Content-Range": f"bytes */{file_size}"
                        }
                        status_res = requests.put(upload_url, headers=status_headers, timeout=30)
                        if status_res.status_code == 308:
                            range_hdr = status_res.headers.get("Range")
                            if range_hdr and "-" in range_hdr:
                                uploaded_bytes = int(range_hdr.split("-")[1]) + 1
                                logging.info(f"Wiederaufnahme bei Byte {uploaded_bytes}...")
                                break  # Raus aus der Chunk-Retry-Schleife, um ab der neuen Position fortzufahren
                        elif status_res.status_code in (200, 201):
                            video_id = status_res.json().get("id")
                            uploaded_bytes = file_size
                            chunk_success = True
                            logging.info(f"Video war bereits vollständig übertragen! Video-ID: {video_id}")
                            break
                    except Exception as query_err:
                        logging.warning(f"Konnte Status nicht abfragen: {query_err}")

            if not chunk_success and video_id is None:
                raise RuntimeError(f"Upload nach {max_retries} Versuchen an Position {uploaded_bytes} abgebrochen.")

    # Thumbnail setzen
    if video_id and thumb_path and os.path.exists(thumb_path):
        try:
            logging.info(f"Lade Thumbnail für Video {video_id} hoch...")
            thumb_url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
            thumb_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "image/jpeg"
            }
            with open(thumb_path, "rb") as tf:
                thumb_res = requests.post(thumb_url, headers=thumb_headers, data=tf.read(), timeout=60)
                if thumb_res.status_code == 200:
                    logging.info("Thumbnail gesetzt.")
                else:
                    logging.warning(f"Thumbnail-Upload fehlgeschlagen ({thumb_res.status_code}): {thumb_res.text}")
        except Exception as e:
            logging.warning(f"Fehler beim Thumbnail-Upload: {e}")

    # Zur Playlist hinzufügen
    if playlist_name and video_id:
        add_video_to_playlist(video_id, playlist_name, access_token, privacy=privacy)

    # Im Browser öffnen
    if video_id and open_link:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logging.info(f"Öffne Video-URL im Browser: {video_url}")
        webbrowser.open(video_url)

    return video_id


# ==========================================
# PROCESS PIPELINE (AUTOMATION / DAEMON)
# ==========================================
def process_upload(args, target_dir=IN_DIR):
    """Verarbeitet Videos und wendet übergebene CLI-Argumente an."""
    input_path = wait_for_input(target_dir)

    # Datei-Integritätsprüfung mit mehreren Versuchen
    is_ready = False
    for check_round in range(5):
        if is_file_ready_and_valid(input_path):
            is_ready = True
            break
        logging.warning(f"Datei noch nicht bereit. Warte 10 Sekunden... (Versuch {check_round + 1}/5)")
        time.sleep(10)

    if not is_ready:
        logging.error(f"Datei unvollständig oder beschädigt. Verschiebe nach corrupt: {input_path}")
        shutil.move(input_path, os.path.join(CORRUPT_DIR, os.path.basename(input_path)))
        return False

    is_symlink = os.path.islink(input_path)
    filename = os.path.basename(input_path)
    work_path = os.path.join(WORK_DIR, filename)

    logging.info(f"Verschiebe nach WORK: {input_path} -> {work_path}")
    shutil.move(input_path, work_path)

    logging.info("Extrahiere Metadaten & Thumbnail...")
    meta = extract_metadata_and_thumb(work_path)

    filename_base = os.path.splitext(filename)[0]

    # Priorität: CLI Argumente > Datei-Metadaten > Default Fallbacks
    title_base = args.title or meta["title"] or filename_base

    # Description Ermittlung
    description = args.description or meta["description"] or DEFAULT_DESCRIPTION
    if args.description_file and os.path.exists(args.description_file):
        with open(args.description_file, "r", encoding="utf-8") as df:
            description = df.read()

    if meta["purl"]:
        description += f"\n\nOriginal-Video-URL: {meta['purl']}"

    tags = args.tags or DEFAULT_TAGS
    rec_date_flag = args.recording_date

    if meta["date"] and not rec_date_flag:
        mdate = str(meta["date"])
        if re.match(r"^\d{8}$", mdate):
            formatted_date = f"{mdate[:4]}-{mdate[4:6]}-{mdate[6:8]}"
            description += f"\n\nAufnahmedatum: {formatted_date}"
            rec_date_flag = f"{formatted_date}T00:00:00.000Z"
            tags += f", {formatted_date}, {mdate[:4]}, {mdate}"

    category = args.category or meta["genre"] or DEFAULT_CATEGORY
    target_playlist = args.playlist or (meta["artist"] if DYNAMIC_PLAYLISTS else None) or PLAYLIST_NAME
    thumb_path = args.thumbnail or meta["thumb_path"]
    cred_path = args.credentials_file or args.client_secrets or CREDENTIALS_FILE

    segments = split_video_if_needed(work_path)
    is_split = len(segments) > 1

    try:
        for idx, seg_file in enumerate(segments, start=1):
            final_title = title_base
            if is_split:
                template = args.title_template or "{title} [{n}/{total}]"
                final_title = template.format(title=title_base, n=idx, total=len(segments))

            upload_single_video(
                file_path=seg_file,
                title=final_title,
                desc=description,
                category=category,
                tags=tags,
                rec_date=rec_date_flag,
                thumb_path=thumb_path,
                playlist_name=target_playlist,
                privacy=args.privacy,
                publish_at=args.publish_at,
                license_type=args.license,
                location=args.location,
                default_lang=args.default_language,
                default_audio_lang=args.default_audio_language,
                embeddable=args.embeddable,
                cred_file=cred_path,
                chunksize=args.chunksize,
                open_link=args.open_link
            )

            # Bei gesplitteten Videos nur die temporären Segmente löschen
            if is_split and os.path.exists(seg_file) and seg_file != work_path:
                try:
                    os.remove(seg_file)
                except OSError:
                    pass

        # Nach erfolgreichem Upload das Thumbnail bereinigen
        if meta["thumb_path"] and os.path.exists(meta["thumb_path"]):
            try:
                os.remove(meta["thumb_path"])
            except OSError:
                pass

        # Archivierung der Originaldatei
        if is_symlink:
            if os.path.exists(work_path):
                os.remove(work_path)
        else:
            if os.path.exists(work_path):
                done_path = os.path.join(DONE_DIR, filename)
                shutil.move(work_path, done_path)
                logging.info(f"Datei erfolgreich archiviert nach: {done_path}")

        return True

    except Exception as e:
        logging.error(f"Fehler während des Uploads von {filename}: {e}", exc_info=True)
        # Wenn Segmente erstellt wurden, aufräumen
        if is_split:
            for seg_file in segments:
                if seg_file != work_path and os.path.exists(seg_file):
                    try:
                        os.remove(seg_file)
                    except OSError:
                        pass
        raise


# ==========================================
# ARGUMENT PARSER & ENTRY POINT
# ==========================================
def parse_args():
    parser = argparse.ArgumentParser(
        description=f"{__title__} v{__version__} (YouTube Data API v3)"
    )

    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit."
    )

    parser.add_argument(
        "-a", "--auto", metavar="PATH", type=str, nargs="?", const=IN_DIR,
        help="Automated mode: processes directory/stream, handles FFmpeg splitting, "
             "metadata inheritance, and sequential uploads. (Default path: /videos/in)"
    )
    parser.add_argument(
        "-D", "--daemon", action="store_true",
        help="Run as a background daemon process to continuously monitor target folders via inotify."
    )
    parser.add_argument("files", nargs="*", help="Video file(s) to upload in manual mode.")

    parser.add_argument("-t", "--title", type=str, help="Video title")
    parser.add_argument("-c", "--category", type=str, help="Name or ID of video category")
    parser.add_argument("-d", "--description", type=str, help="Video description")
    parser.add_argument("--description-file", type=str, help="Path to file containing video description")
    parser.add_argument("--tags", type=str, help='Video tags (comma-separated: "tag1, tag2")')
    parser.add_argument(
        "--privacy", type=str, choices=["public", "unlisted", "private"],
        default=VIDEO_PRIVACY, help=f"Privacy status (default: {VIDEO_PRIVACY})"
    )
    parser.add_argument("--publish-at", type=str, help="Publish date (ISO 8601: YYYY-MM-DDThh:mm:ss.sZ)")
    parser.add_argument(
        "--license", type=str, choices=["youtube", "creativeCommon"],
        default="youtube", help='License for the video ("youtube" or "creativeCommon")'
    )
    parser.add_argument(
        "--location", type=str,
        help='Video location format: "latitude=VAL,longitude=VAL[,altitude=VAL]"'
    )
    parser.add_argument("--recording-date", type=str, help="Recording date (ISO 8601: YYYY-MM-DDThh:mm:ss.sZ)")
    parser.add_argument("--default-language", type=str, default=VIDEO_LANGUAGE, help="Default language code (ISO 639-1)")
    parser.add_argument("--default-audio-language", type=str, default=VIDEO_LANGUAGE, help="Default audio language code (ISO 639-1)")
    parser.add_argument("--thumbnail", type=str, help="Image file to use as video thumbnail (JPEG/PNG)")
    parser.add_argument("--playlist", type=str, help="Playlist title or ID (created if it does not exist)")
    parser.add_argument(
        "--title-template", type=str, default="{title} [{n}/{total}]",
        help="Template for multiple videos (default: {title} [{n}/{total}])"
    )

    parser.add_argument(
        "--embeddable", action=argparse.BooleanOptionalAction, default=True,
        help="Allow video embedding"
    )

    parser.add_argument("--client-secrets", type=str, help="Path to client secrets JSON file")
    # default=None sorgt dafür, dass --client-secrets korrekt als Fallback greift
    parser.add_argument("--credentials-file", type=str, default=None, help="Path to credentials storage JSON file")
    parser.add_argument("--chunksize", type=int, default=104857600, help="Upload file chunksize in bytes (default: 100MB)")
    parser.add_argument("--open-link", action="store_true", help="Open video URL in web browser after upload completes")

    args = parser.parse_args()

    if not args.auto and not args.daemon and not args.files:
        parser.error("You must specify video file(s), use -a/--auto for batch processing, or -D/--daemon for background worker mode.")

    return args


def setup_logging(log_file, log_to_file=True):
    handlers = [logging.StreamHandler(sys.stdout)]

    if log_to_file and log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handlers.append(logging.FileHandler(log_file))
        except (PermissionError, OSError) as e:
            sys.stderr.write(f"Warnung: Log-Datei {log_file} nicht schreibbar ({e}). Logge nur auf stdout.\n")

    log_level = logging.DEBUG if os.environ.get("DEBUG") == "1" else logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True
    )


def main():
    args = parse_args()

    # Logging erst nach args-Parsing aufsetzen
    is_service_mode = bool(args.daemon or args.auto)
    setup_logging(LOG_FILE, log_to_file=is_service_mode)

    # Verzeichnisse nur anlegen, wenn im Service-Modus
    if is_service_mode:
        ensure_directories()

    cred_path = args.credentials_file or args.client_secrets or CREDENTIALS_FILE

    # --- MODUS 1: DAEMON MODUS (-D) ---
    if args.daemon:
        target_dir = args.auto if isinstance(args.auto, str) else IN_DIR
        logging.info(f"Starte Python Upload Worker Daemon auf Verzeichnis: {target_dir}...")
        cleanup_work_dir()
        while True:
            try:
                process_upload(args, target_dir=target_dir)
            except Exception as e:
                logging.error(f"Fehler bei Verarbeitung im Daemon Mode: {e}", exc_info=True)
                cleanup_work_dir()
                time.sleep(10)

    # --- MODUS 2: AUTOMATISCHER BATCH-RUN (-a / --auto) ---
    elif args.auto:
        target_path = args.auto if isinstance(args.auto, str) else IN_DIR
        logging.info(f"Starte automatischen Batch-Upload für Ordner: {target_path}")
        while True:
            found = find_existing_video(target_path)
            if not found:
                logging.info("Keine weiteren Videos im Zielordner gefunden. Batch-Run beendet.")
                break
            try:
                process_upload(args, target_dir=target_path)
            except Exception as e:
                logging.error(f"Fehler bei Batch-Verarbeitung von {found}: {e}", exc_info=True)
                cleanup_work_dir()
                break

    # --- MODUS 3: MANUELLER CLI-UPLOAD ---
    else:
        total_files = len(args.files)
        logging.info(f"Starte manuellen Upload für {total_files} Datei(en)...")

        description_content = args.description
        if args.description_file and os.path.exists(args.description_file):
            with open(args.description_file, "r", encoding="utf-8") as df:
                description_content = df.read()

        for idx, file_path in enumerate(args.files, start=1):
            if not os.path.exists(file_path):
                logging.error(f"Datei nicht gefunden: {file_path}")
                continue

            file_base = os.path.splitext(os.path.basename(file_path))[0]

            if args.title:
                if total_files > 1:
                    title = args.title_template.format(title=args.title, n=idx, total=total_files)
                else:
                    title = args.title
            else:
                title = file_base

            upload_single_video(
                file_path=file_path,
                title=title,
                desc=description_content or DEFAULT_DESCRIPTION,
                category=args.category or DEFAULT_CATEGORY,
                tags=args.tags or DEFAULT_TAGS,
                rec_date=args.recording_date,
                thumb_path=args.thumbnail,
                playlist_name=args.playlist or PLAYLIST_NAME,
                privacy=args.privacy,
                publish_at=args.publish_at,
                license_type=args.license,
                location=args.location,
                default_lang=args.default_language,
                default_audio_lang=args.default_audio_language,
                embeddable=args.embeddable,
                cred_file=cred_path,
                chunksize=args.chunksize,
                open_link=args.open_link
            )


if __name__ == "__main__":
    main()

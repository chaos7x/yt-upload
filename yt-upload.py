#!/usr/bin/env python3
# Copyright (C) 2026 Chaos7x
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.


"""
Automatischer YouTube Upload Worker (FFmpeg / FFprobe / Native REST API).

Überwacht ein Verzeichnis via inotify, liest Metadaten & Thumbnails aus,
zerlegt Videos > 10 Stunden verlustfrei und lädt sie direkt via YouTube Data API v3 hoch.
"""

import json
import logging
import os
if os.environ.get('DEBUG', '').lower() in ('true', 'yes', '1'):
    os.environ['DEBUG'] = '1'
else:
    os.environ['DEBUG'] = '0'
import re
import shutil
import subprocess
import sys
import time
import inotify.adapters
import unicodedata
import requests

# ==========================================
# KONFIGURATION & PATHS
# ==========================================
IN_DIR = "/videos/in"
WORK_DIR = "/videos/work"
DONE_DIR = "/videos/done"
CORRUPT_DIR = "/videos/corrupt"
LOG_FILE = "/log/upload.log"

SEGMENT_TIME_SEC = 36000  # 10 Stunden Limit (in Sekunden)

DEFAULT_DESCRIPTION = "Automatischer Upload via Script."
DEFAULT_TAGS = "Upload, Video"
DEFAULT_CATEGORY = "Entertainment"

DYNAMIC_PLAYLISTS = os.getenv("ENABLE_DYNAMIC_PLAYLISTS", "false").lower() in ("1", "true", "yes")

VIDEO_PRIVACY = "unlisted"  # 'public', 'private', 'unlisted'
VIDEO_LANGUAGE = "de"
ALLOW_EMBEDDING = "True"

CREDENTIALS_FILE = "/app/oauth/youtube-upload-credentials.json"
PLAYLIST_NAME = ""

# Logging aufsetzen
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)


# ==========================================
# OAUTH TOKEN HELPER
# ==========================================
def get_access_token():
    """Liest den Refresh Token aus der JSON-Datei und holt ein frisches Access Token via Google OAuth API."""
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(f"Credentials-Datei nicht gefunden: {CREDENTIALS_FILE}")

    with open(CREDENTIALS_FILE, "r") as f:
        data = json.load(f)

    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    refresh_token = data.get("refresh_token")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("OAuth-Credentials unvollständig (client_id, client_secret oder refresh_token fehlt).")

    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    res = requests.post(token_url, data=payload)
    if res.status_code != 200:
        raise RuntimeError(f"Fehler beim Erneuern des Access Tokens: {res.text}")

    return res.json().get("access_token")


# ==========================================
# HELPER & CLEANUP
# ==========================================
def ensure_directories():
    for d in [IN_DIR, WORK_DIR, DONE_DIR, CORRUPT_DIR]:
        os.makedirs(d, exist_ok=True)


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


def is_file_ready_and_valid(file_path):
    try:
        initial_size = os.path.getsize(file_path)
        time.sleep(2)
        current_size = os.path.getsize(file_path)
        if initial_size != current_size or current_size == 0:
            logging.warning(f"Datei wird eventuell noch geschrieben: {os.path.basename(file_path)}")
            return False
    except Exception as e:
        logging.error(f"Fehler bei Dateigrößenprüfung von {file_path}: {e}")
        return False

    cmd_check = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    res = subprocess.run(cmd_check, capture_output=True, text=True)
    if res.returncode != 0:
        logging.warning(f"FFprobe-Check fehlgeschlagen (moov-Atom fehlt): {os.path.basename(file_path)}")
        return False

    return True


# ==========================================
# INOTIFY & INPUT FINDER
# ==========================================
def find_existing_video():
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")
    for root, _, files in os.walk(IN_DIR):
        for file in files:
            if file.lower().endswith(valid_exts):
                return os.path.join(root, file)
    return None


def wait_for_input():
    existing = find_existing_video()
    if existing:
        logging.info(f"Bestehende Datei gefunden: {existing}")
        return existing

    logging.info("Warte via inotify auf neue Dateien in IN...")
    valid_exts = (".mp4", ".mkv", ".mov", ".m4v")
    i = inotify.adapters.InotifyTree(IN_DIR)

    while True:
        try:
            for event in i.event_gen(yield_nones=False, timeout_s=60):
                if event is None:
                    existing = find_existing_video()
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
            existing = find_existing_video()
            if existing:
                return existing


# ==========================================
# METADATEN & THUMBNAIL
# ==========================================
def get_valid_category_id(category_input):
    """Mappt Textgenres oder Namen auf offizielle YouTube Category IDs."""
    if not category_input:
        return "22" # Standard: People & Blogs
    
    # Falls es schon eine reine Ziffer ist (z.B. "20", "22")
    if str(category_input).isdigit():
        return str(category_input)
    
    # Bekannte Mappings aus Tags/Genres
    cat_lower = str(category_input).lower()
    mapping = {
        "Film & Animation": "1",
        "Autos & Vehicles": "2",
        "Music": "10",
        "Pets & Animals": "15",
        "Sports": "17",
        "Short Movies": "18",
        "Travel & Events": "19",
        "Gaming": "20",
        "Videoblogging": "21",
        "People & Blogs": "22",
        "Comedy": "23",
        "Entertainment": "24",
        "News & Politics": "25",
        "Howto & Style": "26",
        "Education": "27",
        "Science & Technology": "28",
        "Nonprofits & Activism": "29",
        "Movies": "30",
        "Anime/Animation": "31",
        "Action/Adventure": "32",
        "Classics": "33",
        "Documentary": "35",
        "Drama": "36",
        "Family": "37",
        "Foreign": "38",
        "Horror": "39",
        "Sci-Fi/Fantasy": "40",
        "Thriller": "41",
        "Shorts": "42",
        "Shows": "43",
        "Trailers": "44",
    }

    for key, val in mapping.items():
        if key in cat_lower:
            return val
            
    return "22" # Fallback-Standard


def sanitize_text(text):
    if not text:
        return text
    normalized = unicodedata.normalize('NFKD', text)
    cleaned_chars = [c for c in normalized if unicodedata.category(c) not in ('Mn', 'So')]
    result = unicodedata.normalize('NFC', ''.join(cleaned_chars))
    return re.sub(r'\s+', ' ', result).strip()


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

    thumb_path = "/tmp/thumb_temp.jpg"
    temp_attach = "/tmp/attach_temp"

    for p in (thumb_path, temp_attach):
        if os.path.exists(p):
            os.remove(p)

    try:
        cmd_mkv = ["ffmpeg", "-y", "-dump_attachment:t:0", temp_attach, "-i", file_path]
        subprocess.run(cmd_mkv, capture_output=True, text=True)

        if not os.path.exists(temp_attach) or os.path.getsize(temp_attach) == 0:
            cmd_mp4 = ["ffmpeg", "-y", "-i", file_path, "-map", "0:v", "-map", "-0:V", "-c", "copy", temp_attach]
            subprocess.run(cmd_mp4, capture_output=True, text=True)

        if os.path.exists(temp_attach) and os.path.getsize(temp_attach) > 0:
            cmd_conv = ["ffmpeg", "-y", "-i", temp_attach, "-q:v", "2", thumb_path]
            subprocess.run(cmd_conv, capture_output=True, text=True)
            if os.path.exists(temp_attach):
                os.remove(temp_attach)

        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            cmd_frame = ["ffmpeg", "-y", "-ss", "00:00:01", "-i", file_path, "-vframes", "1", "-q:v", "2", thumb_path]
            subprocess.run(cmd_frame, capture_output=True, text=True)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            metadata["thumb_path"] = thumb_path
            logging.info("Thumbnail erfolgreich extrahiert.")

    except Exception as e:
        logging.warning(f"Konnte kein Thumbnail extrahieren: {e}")

    return metadata


# ==========================================
# FFMPEG LOSSLESS SPLITTER
# ==========================================
def split_video_if_needed(work_path):
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

    if os.path.exists(work_path):
        os.remove(work_path)

    return created_segments


# ==========================================
# NATIVE REST API UPLOADER & PLAYLIST HELPER
# ==========================================
def add_video_to_playlist(video_id, playlist_name, access_token):
    if not playlist_name or not video_id:
        return False

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        # 1. Playlist suchen
        playlist_id = None
        list_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50"
        res = requests.get(list_url, headers=headers)
        if res.status_code == 200:
            for item in res.json().get("items", []):
                if item["snippet"]["title"].lower() == playlist_name.lower():
                    playlist_id = item["id"]
                    break

        # 2. Playlist anlegen falls nicht vorhanden
        if not playlist_id:
            create_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet,status"
            create_body = {
                "snippet": {"title": playlist_name, "description": "Automatisch erstellt"},
                "status": {"privacyStatus": VIDEO_PRIVACY}
            }
            create_res = requests.post(create_url, headers=headers, json=create_body)
            if create_res.status_code in (200, 201):
                playlist_id = create_res.json().get("id")

        # 3. Video zur Playlist hinzufügen
        if playlist_id:
            item_url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
            item_body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id}
                }
            }
            item_res = requests.post(item_url, headers=headers, json=item_body)
            if item_res.status_code in (200, 201):
                logging.info(f"Video {video_id} erfolgreich zur Playlist '{playlist_name}' hinzugefügt.")
                return True

    except Exception as e:
        logging.error(f"Fehler bei Playlist-API: {e}")
    return False


def upload_single_video(file_path, title, desc, category, tags, rec_date, thumb_path, playlist_name):
    logging.info(f"Lade hoch via native HTTP REST API ({VIDEO_PRIVACY}): {os.path.basename(file_path)}")

    file_size = os.path.getsize(file_path)
    access_token = get_access_token()

    # Metadaten-Body aufbauen (inklusive recordingDetails)[cite: 1]
    metadata_body = {
        "snippet": {
            "title": title,
            "description": desc,
            "categoryId": get_valid_category_id(category),
            "tags": [t.strip() for t in tags.split(",")] if tags else [],
            "defaultLanguage": VIDEO_LANGUAGE,
            "defaultAudioLanguage": VIDEO_LANGUAGE,
        },
        "status": {
            "privacyStatus": VIDEO_PRIVACY,
            "embeddable": ALLOW_EMBEDDING,
        }
    }

    if rec_date:
        metadata_body["recordingDetails"] = {
            "recordingDate": rec_date  # Format: "YYYY-MM-DDTHH:MM:SS.000Z"[cite: 1]
        }

    init_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status,recordingDetails"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/*"
    }

    max_retries = 3
    video_id = None

    for attempt in range(1, max_retries + 1):
        try:
            logging.info("Initialisiere Resumable Upload Session...")
            init_res = requests.post(init_url, headers=headers, json=metadata_body)
            if init_res.status_code != 200:
                raise RuntimeError(f"Init fehlgeschlagen ({init_res.status_code}): {init_res.text}")

            upload_url = init_res.headers.get("Location")
            if not upload_url:
                raise RuntimeError("Keine Upload-Location im Header erhalten.")

            chunk_size = 104857600  # 100 MB Chunks
            logging.info(f"Starte Chunk-Upload ({file_size / 1024 / 1024:.2f} MB)...")

            with open(file_path, "rb") as f:
                uploaded_bytes = 0
                while uploaded_bytes < file_size:
                    chunk = f.read(chunk_size)
                    chunk_len = len(chunk)
                    start_byte = uploaded_bytes
                    end_byte = uploaded_bytes + chunk_len - 1

                    chunk_headers = {
                        "Authorization": f"Bearer {access_token}",
                        "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                        "Content-Type": "video/*"
                    }

                    logging.info(f"Sende Bytes {start_byte}-{end_byte}/{file_size}...")
                    put_res = requests.put(upload_url, headers=chunk_headers, data=chunk)

                    if put_res.status_code in (200, 201):
                        resp_data = put_res.json()
                        video_id = resp_data.get("id")
                        logging.info(f"Upload ERFOLGREICH! Video-ID: {video_id}")
                        break
                    elif put_res.status_code == 308:
                        uploaded_bytes += chunk_len
                    else:
                        raise RuntimeError(f"Fehler beim Chunk-Upload ({put_res.status_code}): {put_res.text}")

            if video_id:
                break

        except Exception as e:
            logging.warning(f"Upload-Versuch {attempt}/{max_retries} fehlgeschlagen: {e}")
            if attempt == max_retries:
                raise
            time.sleep(attempt * 15)
            access_token = get_access_token()  # Token erneuern bei Retry

    # Thumbnail nachträglich hochladen
    if video_id and thumb_path and os.path.exists(thumb_path):
        try:
            logging.info(f"Lade Thumbnail für Video {video_id} hoch...")
            thumb_url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
            thumb_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "image/jpeg"
            }
            with open(thumb_path, "rb") as tf:
                thumb_res = requests.post(thumb_url, headers=thumb_headers, data=tf.read())
                if thumb_res.status_code == 200:
                    logging.info("Thumbnail gesetzt.")
                else:
                    logging.warning(f"Thumbnail-Upload fehlgeschlagen: {thumb_res.text}")
        except Exception as e:
            logging.warning(f"Fehler beim Thumbnail-Upload: {e}")

    # Playlist-Zuweisung
    if playlist_name and video_id:
        add_video_to_playlist(video_id, playlist_name, access_token)

    return video_id


# ==========================================
# PROCESS PIPELINE
# ==========================================
def process_upload():
    input_path = wait_for_input()

    if not is_file_ready_and_valid(input_path):
        logging.warning("Datei noch nicht bereit. Warte 15 Sekunden...")
        time.sleep(15)
        if not is_file_ready_and_valid(input_path):
            logging.error(f"Datei beschädigt. Verschiebe nach corrupt: {input_path}")
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
    title_base = meta["title"] or filename_base
    description = meta["description"] or DEFAULT_DESCRIPTION

    if meta["purl"]:
        description += f"\n\nOriginal-Video-URL: {meta['purl']}"

    tags = DEFAULT_TAGS
    rec_date_flag = None

    if meta["date"]:
        mdate = str(meta["date"])
        if re.match(r"^\d{8}$", mdate):
            formatted_date = f"{mdate[:4]}-{mdate[4:6]}-{mdate[6:8]}"
            description += f"\n\nAufnahmedatum: {formatted_date}"
            rec_date_flag = f"{formatted_date}T00:00:00.000Z"  # ISO-8601 Format für YouTube API[cite: 1]
            tags += f", {formatted_date}, {mdate[:4]}, {mdate}"
        else:
            description += f"\n\nDatum: {mdate}"
            tags += f", {mdate}"

    category = meta["genre"] or DEFAULT_CATEGORY
    target_playlist = (meta["artist"] if DYNAMIC_PLAYLISTS else None) or PLAYLIST_NAME

    segments = split_video_if_needed(work_path)

    for idx, seg_file in enumerate(segments, start=1):
        final_title = title_base
        if len(segments) > 1:
            final_title = f"{title_base} (Teil {idx:02d})"

        upload_single_video(
            seg_file,
            final_title,
            description,
            category,
            tags,
            rec_date_flag,
            meta["thumb_path"],
            target_playlist,
        )

        if len(segments) > 1 and os.path.exists(seg_file):
            os.remove(seg_file)

    if meta["thumb_path"] and os.path.exists(meta["thumb_path"]):
        os.remove(meta["thumb_path"])

    if is_symlink:
        if os.path.exists(work_path):
            os.remove(work_path)
    else:
        if os.path.exists(work_path):
            done_path = os.path.join(DONE_DIR, filename)
            shutil.move(work_path, done_path)
            logging.info(f"Datei archiviert nach: {done_path}")

    return True


# ==========================================
# MAIN DAEMON LOOP
# ==========================================
def main():
    ensure_directories()
    logging.info("Starte Python Upload Worker Daemon (Native HTTP)...")

    while True:
        try:
            process_upload()
        except Exception as e:
            logging.error(f"Fehler bei Verarbeitung: {e}", exc_info=True)
            cleanup_work_dir()
            time.sleep(10)


if __name__ == "__main__":
    main()

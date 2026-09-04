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
Automatischer YouTube Upload Worker (Native HTTP / FFmpeg / FFprobe).

Überwacht ein Verzeichnis via inotify.adapters (python3-inotify),
liest Metadaten & Thumbnails via FFprobe/FFmpeg aus, zerlegt Videos > 10 Stunden
verlustfrei in Segmente (-c copy) und lädt sie direkt via REST API (Resumable Upload)
auf YouTube hoch – völlig unabhängig von youtube-upload.
"""

import json
import logging
import os

# Muss vor dem Import von inotify stehen, da die Bibliothek
# os.environ.get('DEBUG') ungesichert als int() auswertet.
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

# Einzige externe HTTP-Bibliothek für die REST-API
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
DEFAULT_TAGS = ["Upload", "Video"]
DEFAULT_CATEGORY_ID = "24"  # 24 = Entertainment (YouTube Category ID)

# Feature-Flags / Environment (Standard: off)
DYNAMIC_PLAYLISTS = os.getenv("ENABLE_DYNAMIC_PLAYLISTS", "false").lower() in ("1", "true", "yes")

VIDEO_PRIVACY = "unlisted"  # 'public', 'private', 'unlisted'
VIDEO_LANGUAGE = "de"

CLIENT_SECRETS = "/app/oauth/client_secrets.json"
CREDENTIALS_FILE = "/app/oauth/youtube-upload-credentials.json"
PLAYLIST_NAME = ""  # Fallback-Playlist (leer lassen, wenn ohne Tag keine Playlist genutzt werden soll)

CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB Upload-Chunks (Vielfaches von 256 KB)

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
        logging.warning(f"FFprobe-Check fehlgeschlagen (unvollständig / moov-Atom fehlt): {os.path.basename(file_path)}")
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
# METADATEN & THUMBNAIL (FFPROBE / FFMPEG)
# ==========================================
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
            cmd_mp4 = [
                "ffmpeg", "-y", "-i", file_path,
                "-map", "0:v", "-map", "-0:V",
                "-c", "copy", temp_attach
            ]
            subprocess.run(cmd_mp4, capture_output=True, text=True)

        if os.path.exists(temp_attach) and os.path.getsize(temp_attach) > 0:
            cmd_conv = ["ffmpeg", "-y", "-i", temp_attach, "-q:v", "2", thumb_path]
            subprocess.run(cmd_conv, capture_output=True, text=True)
            if os.path.exists(temp_attach):
                os.remove(temp_attach)

        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            cmd_frame = [
                "ffmpeg", "-y", "-ss", "00:00:01",
                "-i", file_path, "-vframes", "1",
                "-q:v", "2", thumb_path
            ]
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
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            work_path
        ]
        res = subprocess.run(cmd_probe, capture_output=True, text=True, check=True)
        duration_sec = float(res.stdout.strip())
    except Exception as e:
        logging.error(f"Fehler bei Dauer-Ermittlung vor Splitting: {e}")
        return [work_path]

    logging.info(f"Videolänge: {int(duration_sec)} Sekunden ({duration_sec / 3600:.2f} Stunden)")

    if duration_sec <= SEGMENT_TIME_SEC:
        return [work_path]

    logging.info("Video ist länger als 10 Stunden. Starte verlustfreies FFmpeg-Splitting...")

    filename = os.path.basename(work_path)
    base_name, ext = os.path.splitext(filename)
    segment_pattern = os.path.join(WORK_DIR, f"{base_name}_part%02d{ext}")

    cmd_split = [
        "ffmpeg", "-y", "-i", work_path,
        "-c", "copy", "-map", "0",
        "-avoid_negative_ts", "make_zero",
        "-f", "segment",
        "-segment_time", str(SEGMENT_TIME_SEC),
        "-reset_timestamps", "1",
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

    logging.info(f"Segmentierung abgeschlossen. {len(created_segments)} Segmente erzeugt.")
    return created_segments


# ==========================================
# NATIVE REST API (OAUTH2, UPLOAD & PLAYLIST)
# ==========================================
def get_access_token():
    """Hole frischen OAuth2 Access Token über den gespeicherten Refresh Token."""
    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            creds = json.load(f)

        refresh_token = creds.get("refresh_token")
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")

        if not client_id or not client_secret:
            with open(CLIENT_SECRETS, 'r') as f:
                secrets = json.load(f).get("installed", {})
                client_id = secrets.get("client_id")
                client_secret = secrets.get("client_secret")

        url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }

        res = requests.post(url, data=data, timeout=30)
        res.raise_for_status()
        return res.json()["access_token"]

    except Exception as e:
        logging.error(f"Fehler beim Erneuern des OAuth2 Tokens: {e}")
        raise


def add_video_to_playlist_native(access_token, video_id, playlist_name):
    """Fügt ein Video nativ per REST-API einer Playlist hinzu."""
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        # 1. Suchen
        list_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50"
        res = requests.get(list_url, headers=headers, timeout=30)
        playlist_id = None

        if res.status_code == 200:
            for item in res.json().get("items", []):
                if item["snippet"]["title"].lower() == playlist_name.lower():
                    playlist_id = item["id"]
                    break

        # 2. Erstellen falls nicht vorhanden
        if not playlist_id:
            logging.info(f"Erstelle neue Playlist via REST API: {playlist_name}")
            create_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet,status"
            create_body = {
                "snippet": {"title": playlist_name, "description": "Automatisch erstellt"},
                "status": {"privacyStatus": VIDEO_PRIVACY}
            }
            res_create = requests.post(create_url, headers=headers, json=create_body, timeout=30)
            if res_create.status_code in (200, 201):
                playlist_id = res_create.json()["id"]

        # 3. Hinzufügen
        if playlist_id:
            item_url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
            item_body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id}
                }
            }
            res_item = requests.post(item_url, headers=headers, json=item_body, timeout=30)
            if res_item.status_code in (200, 201):
                logging.info(f"Video {video_id} zur Playlist '{playlist_name}' hinzugefügt.")
            else:
                logging.warning(f"Playlist-Zuordnung fehlgeschlagen: {res_item.text}")

    except Exception as e:
        logging.error(f"Fehler bei Playlist-REST-API: {e}")


def upload_single_video(
    file_path, title, desc, category, tags, rec_date, thumb_path, playlist_name
):
    """Lädt ein Video nativ per HTTP Resumable Upload hoch."""
    logging.info(f"Lade hoch via native HTTP REST API ({VIDEO_PRIVACY}): {os.path.basename(file_path)}")

    access_token = get_access_token()
    file_size = os.path.getsize(file_path)

    # Convert tags string to list if necessary
    if isinstance(tags, str):
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        tags_list = tags or DEFAULT_TAGS

    # 1. Resumable Session initiieren
    init_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4"
    }

    body = {
        "snippet": {
            "title": title,
            "description": desc,
            "categoryId": str(DEFAULT_CATEGORY_ID),
            "tags": tags_list,
            "defaultLanguage": VIDEO_LANGUAGE,
            "defaultAudioLanguage": VIDEO_LANGUAGE
        },
        "status": {
            "privacyStatus": VIDEO_PRIVACY,
            "embeddable": True
        }
    }

    if rec_date:
        body["snippet"]["recordingDate"] = rec_date

    logging.info("Initialisiere Resumable Upload Session...")
    res_init = requests.post(init_url, headers=headers, json=body, timeout=30)

    if res_init.status_code != 200:
        logging.error(f"API Init Fehlgeschlagen: {res_init.status_code} - {res_init.text}")
        raise RuntimeError(f"API Init Failed: {res_init.text}")

    upload_url = res_init.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Keine Upload-Location erhalten!")

    # 2. Chunks hochladen
    logging.info(f"Starte Chunk-Upload ({file_size / (1024*1024):.2f} MB)...")
    video_id = None

    with open(file_path, "rb") as f:
        offset = 0
        while offset < file_size:
            chunk = f.read(CHUNK_SIZE)
            chunk_len = len(chunk)
            start_byte = offset
            end_byte = offset + chunk_len - 1

            chunk_headers = {
                "Content-Length": str(chunk_len),
                "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}"
            }

            logging.info(f"Sende Bytes {start_byte}-{end_byte}/{file_size}...")

            for attempt in range(1, 4):
                try:
                    res_chunk = requests.put(upload_url, headers=chunk_headers, data=chunk, timeout=300)
                    if res_chunk.status_code in (200, 201):
                        video_id = res_chunk.json().get("id")
                        logging.info(f"Upload ERFOLGREICH! Video-ID: {video_id}")
                        break
                    elif res_chunk.status_code == 308:
                        break  # Chunk erfolgreich akzeptiert
                    else:
                        logging.warning(f"Status {res_chunk.status_code} bei Chunk. Versuch {attempt}/3")
                        time.sleep(5)
                except requests.RequestException as e:
                    logging.warning(f"Netzwerkfehler: {e}. Versuch {attempt}/3")
                    time.sleep(10)

            offset += chunk_len

    if not video_id:
        raise RuntimeError("Upload beendet, aber keine Video-ID erhalten.")

    # 3. Thumbnail setzen
    if thumb_path and os.path.exists(thumb_path) and video_id:
        try:
            logging.info(f"Lade Thumbnail für Video {video_id} hoch...")
            thumb_url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
            thumb_headers = {"Authorization": f"Bearer {access_token}"}
            with open(thumb_path, "rb") as tf:
                res_thumb = requests.post(thumb_url, headers=thumb_headers, files={"media": tf}, timeout=60)
                if res_thumb.status_code in (200, 201):
                    logging.info("Thumbnail gesetzt.")
        except Exception as e:
            logging.warning(f"Thumbnail-Upload Fehler: {e}")

    # 4. Playlist zuweisen
    if playlist_name and video_id:
        add_video_to_playlist_native(access_token, video_id, playlist_name)

    return video_id


# ==========================================
# PROCESS PIPELINE
# ==========================================
def process_upload():
    input_path = wait_for_input()

    if not is_file_ready_and_valid(input_path):
        logging.warning("Datei noch nicht bereit/valide. Warte 15 Sekunden vor Re-Check...")
        time.sleep(15)

        if not is_file_ready_and_valid(input_path):
            logging.error(f"Datei dauerhaft beschädigt oder unvollständig. Verschiebe nach corrupt: {input_path}")
            corrupt_path = os.path.join(CORRUPT_DIR, os.path.basename(input_path))
            shutil.move(input_path, corrupt_path)
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

    tags = DEFAULT_TAGS.copy()
    rec_date_flag = None

    if meta["date"]:
        mdate = str(meta["date"])
        if re.match(r"^\d{8}$", mdate):
            formatted_date = f"{mdate[:4]}-{mdate[4:6]}-{mdate[6:8]}"
            description += f"\n\nAufnahmedatum: {formatted_date}"
            rec_date_flag = f"{formatted_date}T00:00:00.000Z"
            tags.extend([formatted_date, mdate[:4], mdate])
        else:
            description += f"\n\nDatum: {mdate}"
            tags.append(mdate)

    category = meta["genre"] or DEFAULT_CATEGORY_ID

    artist_playlist = meta["artist"] if DYNAMIC_PLAYLISTS else None
    target_playlist = artist_playlist or PLAYLIST_NAME

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
        logging.info("Symlink aufgeräumt.")
    else:
        if os.path.exists(work_path):
            done_path = os.path.join(DONE_DIR, filename)
            shutil.move(work_path, done_path)
            logging.info(f"Datei archiviert nach: {done_path}")
        else:
            logging.info("Originaldatei wurde im Rahmen des Splittings verarbeitet und aufgeräumt.")

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

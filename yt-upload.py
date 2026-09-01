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
Automatischer YouTube Upload Worker (FFmpeg / FFprobe native).

Überwacht ein Verzeichnis via inotify.adapters (python3-inotify),
liest Metadaten & Thumbnails via FFprobe/FFmpeg aus, zerlegt Videos > 10 Stunden
verlustfrei in Segmente (-c copy) und lädt sie auf YouTube hoch.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import inotify.adapters

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

# Feature-Flags / Environment (Standard: off)
DYNAMIC_PLAYLISTS = os.getenv("ENABLE_DYNAMIC_PLAYLISTS", "false").lower() in ("1", "true", "yes")

VIDEO_PRIVACY = "unlisted"  # 'public', 'private', 'unlisted'
VIDEO_LANGUAGE = "de"
ALLOW_EMBEDDING = "True"

CLIENT_SECRETS = "/app/oauth/client_secrets.json"
CREDENTIALS_FILE = "/app/oauth/youtube-upload-credentials.json"
PLAYLIST_NAME = ""  # Fallback-Playlist (leer lassen, wenn ohne Tag keine Playlist genutzt werden soll)

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
    """
    Prüft, ob die Datei stabil ist (Dateigröße verändert sich nicht mehr)
    und ob FFprobe den Container/moov-Atom fehlerfrei auslesen kann.
    """
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
                    # Stiller Fallback-Check ohne Log-Spam
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
def extract_metadata_and_thumb(file_path):
    metadata = {
        "title": None,
        "description": None,
        "purl": None,
        "genre": None,
        "date": None,
        "artist": None,  # Neu: Artist-Tag für dynamische Playlists
        "thumb_path": None,
        "duration": 0,
    }

    # 1. Metadaten & Gesamtdauer via FFprobe auslesen
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

        metadata["title"] = tags.get("TITLE")
        metadata["description"] = tags.get("DESCRIPTION") or tags.get("COMMENT")
        metadata["purl"] = tags.get("PURL")
        metadata["genre"] = tags.get("GENRE")
        metadata["date"] = tags.get("DATE")
        metadata["artist"] = tags.get("ARTIST") or tags.get("ALBUM_ARTIST")

    except Exception as e:
        logging.error(f"Fehler beim Auslesen der Metadaten via FFprobe: {e}")

    # 2. Thumbnail extrahieren (MKV, MP4/MOV/M4V & Fallback)
    thumb_path = "/tmp/thumb_temp.jpg"
    temp_attach = "/tmp/attach_temp"

    for p in (thumb_path, temp_attach):
        if os.path.exists(p):
            os.remove(p)

    try:
        # 2a. Versuch: MKV-Attachment dumpen (WebP/PNG/JPG)
        cmd_mkv = [
            "ffmpeg",
            "-y",
            "-dump_attachment:t:0", temp_attach,
            "-i", file_path
        ]
        subprocess.run(cmd_mkv, capture_output=True, text=True)

        # 2b. Versuch: MP4/MOV/M4V Cover-Stream extrahieren (attached_pic / covr atom)
        if not os.path.exists(temp_attach) or os.path.getsize(temp_attach) == 0:
            cmd_mp4 = [
                "ffmpeg",
                "-y",
                "-i", file_path,
                "-map", "0:v",
                "-map", "-0:V",  # Filtert gezielt auf Still-Images/Cover, ignoriert Haupt-Video
                "-c", "copy",
                temp_attach
            ]
            subprocess.run(cmd_mp4, capture_output=True, text=True)

        # Wenn ein Cover (aus MKV oder MP4) gefunden wurde: Saubere Konvertierung zu JPEG
        if os.path.exists(temp_attach) and os.path.getsize(temp_attach) > 0:
            cmd_conv = [
                "ffmpeg",
                "-y",
                "-i", temp_attach,
                "-q:v", "2",
                thumb_path
            ]
            subprocess.run(cmd_conv, capture_output=True, text=True)
            if os.path.exists(temp_attach):
                os.remove(temp_attach)

        # 2c. Fallback: Frame nach 1 Sekunde grabben, falls überhaupt kein Cover vorhanden war
        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            cmd_frame = [
                "ffmpeg",
                "-y",
                "-ss", "00:00:01",
                "-i", file_path,
                "-vframes", "1",
                "-q:v", "2",
                thumb_path
            ]
            subprocess.run(cmd_frame, capture_output=True, text=True)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            metadata["thumb_path"] = thumb_path
            logging.info("Thumbnail erfolgreich extrahiert und als JPEG aufbereitet.")

    except Exception as e:
        logging.warning(f"Konnte kein Thumbnail extrahieren: {e}")

    return metadata


# ==========================================
# FFMPEG LOSSLESS SPLITTER
# ==========================================
def split_video_if_needed(work_path):
    # Gesamtdauer ermitteln
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
        logging.error(f"Fehler bei Dauer-Ermittlung vor Splitting: {e}")
        return [work_path]

    logging.info(
        f"Videolänge: {int(duration_sec)} Sekunden ({duration_sec / 3600:.2f} Stunden)"
    )

    if duration_sec <= SEGMENT_TIME_SEC:
        return [work_path]

    logging.info("Video ist länger als 10 Stunden. Starte verlustfreies FFmpeg-Splitting...")

    filename = os.path.basename(work_path)
    base_name, ext = os.path.splitext(filename)
    segment_pattern = os.path.join(WORK_DIR, f"{base_name}_part%02d{ext}")

    # FFmpeg Stream Copy Segmentierung
    cmd_split = [
        "ffmpeg",
        "-y",
        "-i", work_path,
        "-c", "copy",
        "-map", "0",
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

    # Generierte Segmente suchen
    created_segments = []
    part_idx = 1
    while True:
        seg_candidate = os.path.join(WORK_DIR, f"{base_name}_part{part_idx:02d}{ext}")
        if os.path.exists(seg_candidate):
            created_segments.append(seg_candidate)
            part_idx += 1
        else:
            break

    # Original unzerlegte Datei in WORK löschen
    if os.path.exists(work_path):
        os.remove(work_path)

    logging.info(
        f"Segmentierung abgeschlossen. {len(created_segments)} Segmente erzeugt."
    )
    return created_segments


# ==========================================
# YOUTUBE UPLOADER CLI
# ==========================================
def get_auth_flags():
    return [
        f"--client-secrets={CLIENT_SECRETS}",
        f"--credentials-file={CREDENTIALS_FILE}",
    ]


def upload_single_video(
    file_path, title, desc, category, tags, rec_date, thumb_path, playlist_name
):
    logging.info(f"Lade hoch ({VIDEO_PRIVACY}): {os.path.basename(file_path)}")

    cmd = [
        "youtube-upload",
        *get_auth_flags(),
        f"--title={title}",
        f"--description={desc}",
        f"--category={category}",
        f"--tags={tags}",
        f"--privacy={VIDEO_PRIVACY}",
        f"--default-language={VIDEO_LANGUAGE}",
        f"--default-audio-language={VIDEO_LANGUAGE}",
        f"--embeddable={ALLOW_EMBEDDING}",
        "--chunksize=104857600",
    ]

    if playlist_name:
        cmd.append(f"--playlist={playlist_name}")
        logging.info(f"Weist Playlist zu (wird ggf. erstellt): {playlist_name}")

    if rec_date:
        cmd.append(f"--recording-date={rec_date}")
    if thumb_path and os.path.exists(thumb_path):
        cmd.append(f"--thumbnail={thumb_path}")

    cmd.append(file_path)

    # Retry-Schleife bei Netzwerkfehlern / Exit-Code 3
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            video_id = res.stdout.strip()
            logging.info(f"Upload erfolgreich! Video ID: {video_id}")
            return video_id
        except subprocess.CalledProcessError as e:
            logging.warning(f"Upload fehlgeschlagen (Versuch {attempt}/{max_retries}), Exit-Code: {e.returncode}")
            if e.stderr:
                logging.warning(f"Fehlerausgabe: {e.stderr.strip()}")
            
            if attempt == max_retries:
                raise
            
            wait_time = attempt * 15
            logging.info(f"Warte {wait_time} Sekunden vor erneutem Versuch...")
            time.sleep(wait_time)


# ==========================================
# PROCESS PIPELINE
# ==========================================
def process_upload():
    input_path = wait_for_input()

    # Prüfung auf Vollständigkeit und intaktes moov-Atom
    if not is_file_ready_and_valid(input_path):
        logging.warning("Datei noch nicht bereit/valide. Warte 15 Sekunden vor Re-Check...")
        time.sleep(15)

        if not is_file_ready_and_valid(input_path):
            logging.error(f"Datei dauerhaft beschädigt oder unvollständig (moov-Atom fehlt). Verschiebe nach corrupt: {input_path}")
            corrupt_path = os.path.join(CORRUPT_DIR, os.path.basename(input_path))
            shutil.move(input_path, corrupt_path)
            return False

    is_symlink = os.path.islink(input_path)
    filename = os.path.basename(input_path)
    work_path = os.path.join(WORK_DIR, filename)

    logging.info(f"Verschiebe nach WORK: {input_path} -> {work_path}")
    shutil.move(input_path, work_path)

    # Metadaten holen
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
            rec_date_flag = f"{formatted_date}T00:00:00.000Z"
            tags += f", {formatted_date}, {mdate[:4]}, {mdate}"
        else:
            description += f"\n\nDatum: {mdate}"
            tags += f", {mdate}"

    category = meta["genre"] or DEFAULT_CATEGORY

    # Dynamische Playlist-Ermittlung (abhängig vom Feature-Flag)
    artist_playlist = meta["artist"] if DYNAMIC_PLAYLISTS else None
    target_playlist = artist_playlist or PLAYLIST_NAME

    # Segmentierung durchführen falls > 10 Std.
    segments = split_video_if_needed(work_path)

    # Uploads abarbeiten
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

    # Thumbnail aufräumen
    if meta["thumb_path"] and os.path.exists(meta["thumb_path"]):
        os.remove(meta["thumb_path"])

    # Archivieren / Aufräumen der ursprünglichen Datei
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
            logging.info(
                "Originaldatei wurde im Rahmen des Splittings verarbeitet und aufgeräumt."
            )

    return True


# ==========================================
# MAIN DAEMON LOOP
# ==========================================
def main():
    ensure_directories()
    logging.info("Starte Python Upload Worker Daemon...")

    while True:
        try:
            process_upload()
        except Exception as e:
            logging.error(f"Fehler bei Verarbeitung: {e}", exc_info=True)
            cleanup_work_dir()
            time.sleep(10)


if __name__ == "__main__":
    main()

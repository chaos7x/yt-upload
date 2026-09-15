"""FFmpeg/FFprobe-Integration: Metadaten-Extraktion, Thumbnails, Video-Splitting."""

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import time

from yt_upload import config
from yt_upload.text_utils import sanitize_text, truncate_title

logger = logging.getLogger(__name__)


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
            logger.error(f"Fehler bei Dateigrößenprüfung von {file_path}: {e}")
            return False

        if current_size == 0:
            logger.warning(f"Datei ist noch 0 Bytes groß, warte... ({check_num + 1}/{max_checks})")
            time.sleep(wait_interval)
            continue

        if last_size != -1 and current_size == last_size:
            break
        elif last_size != -1:
            logger.info(f"Datei wächst noch ({current_size / (1024 * 1024):.1f} MB)... warte weiter.")

        last_size = current_size
        time.sleep(wait_interval)
    else:
        logger.warning(f"Datei wird nach {max_checks * wait_interval}s noch geschrieben: {os.path.basename(file_path)}")
        return False

    # Integritätsprüfung via ffprobe durchführen
    cmd_check = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    res = subprocess.run(cmd_check, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        logger.warning(f"FFprobe-Check fehlgeschlagen (evtl. unvollständig oder ungültiges Format): {os.path.basename(file_path)}")
        return False

    return True


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

    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Fehler beim Auslesen der Metadaten via FFprobe: {e}")

    temp_dir = tempfile.gettempdir()
    pid = os.getpid()
    timestamp = int(time.time() * 1000)
    thumb_path = os.path.join(temp_dir, f"yt_thumb_{pid}_{timestamp}.jpg")
    temp_attach = os.path.join(temp_dir, f"yt_attach_{pid}_{timestamp}.jpg")

    # Versuche eingebettete Cover-Bilder zu extrahieren (MKV / MP4 Attachments)
    try:
        cmd_mkv = ["ffmpeg", "-y", "-dump_attachment:t:0", temp_attach, "-i", file_path]
        subprocess.run(cmd_mkv, capture_output=True, text=True, check=False)

        if not os.path.exists(temp_attach) or os.path.getsize(temp_attach) == 0:
            cmd_mp4 = [
                "ffmpeg", "-y", "-i", file_path,
                "-map", "0:v:m:attached_pic:0?", "-c", "copy", temp_attach
            ]
            subprocess.run(cmd_mp4, capture_output=True, text=True, check=False)

        if os.path.exists(temp_attach) and os.path.getsize(temp_attach) > 0:
            cmd_conv = ["ffmpeg", "-y", "-i", temp_attach, "-q:v", "2", thumb_path]
            subprocess.run(cmd_conv, capture_output=True, text=True, check=False)

        # Fallback: Frame an bestimmter Position im Video als Thumbnail rendern
        if (not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0) and config.AUTO_GENERATE_THUMBNAIL:
            duration = metadata["duration"]
            
            if duration <= 0:
                seek_sec = config.AUTO_THUMB_MIN_SEC
            elif duration < config.AUTO_THUMB_MIN_SEC:
                seek_sec = max(1, duration // 2)
            else:
                target_sec = config.AUTO_THUMB_MIN_SEC + ((config.AUTO_THUMB_MAX_SEC - config.AUTO_THUMB_MIN_SEC) // 2)
                seek_sec = min(target_sec, max(1, duration - 1))

            logger.info(f"Generiere Auto-Thumbnail bei Sekunde {seek_sec} (Video-Dauer: {duration}s)...")

            cmd_frame = [
                "ffmpeg", "-y",
                "-ss", str(seek_sec),
                "-i", file_path,
                "-frames:v", "1",
                "-q:v", "2",
                thumb_path
            ]
            subprocess.run(cmd_frame, capture_output=True, text=True, check=False)

            if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
                logger.warning("Seek fehlgeschlagen, versuche ersten Keyframe zu greifen...")
                cmd_keyframe = [
                    "ffmpeg", "-y",
                    "-discard", "nokey",
                    "-i", file_path,
                    "-frames:v", "1",
                    "-q:v", "2",
                    thumb_path
                ]
                subprocess.run(cmd_keyframe, capture_output=True, text=True, check=False)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            metadata["thumb_path"] = thumb_path
            logger.info(f"Thumbnail erfolgreich zugewiesen: {thumb_path}")
        else:
            logger.warning("Kein Thumbnail generiert/gefunden. YouTube wird ein automatisches Frame wählen.")

    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"Fehler bei der Thumbnail-Extraktion: {e}")
    finally:
        # Aufräumen der temporären Attachment-Datei
        if os.path.exists(temp_attach):
            with contextlib.suppress(OSError):
                os.remove(temp_attach)

    return metadata


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
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        logger.error(f"Fehler bei Dauer-Ermittlung: {e}")
        return [work_path]

    logger.info(f"Videolänge: {int(duration_sec)} Sekunden ({duration_sec / 3600:.2f} Stunden)")

    if duration_sec <= config.SEGMENT_TIME_SEC:
        return [work_path]

    logger.info("Video überschreitet 10 Stunden. Starte FFmpeg-Splitting...")
    filename = os.path.basename(work_path)
    base_name, ext = os.path.splitext(filename)
    segment_pattern = os.path.join(config.WORK_DIR, f"{base_name}_part%02d{ext}")

    # Alte Segmente entfernen, falls noch vorhanden
    for item in os.listdir(config.WORK_DIR):
        if item.startswith(f"{base_name}_part") and item.endswith(ext):
            stale_segment = os.path.join(config.WORK_DIR, item)
            try:
                os.remove(stale_segment)
            except OSError as e:
                raise RuntimeError(f"Altes Segment kann nicht gelöscht werden: {stale_segment}") from e

    # Segmentierung verlustfrei mit Stream-Copy ausführen
    cmd_split = [
        "ffmpeg", "-y", "-i", work_path, "-c", "copy", "-map", "0",
        "-avoid_negative_ts", "make_zero", "-f", "segment",
        "-segment_time", str(config.SEGMENT_TIME_SEC), "-reset_timestamps", "1",
        segment_pattern
    ]

    try:
        subprocess.run(cmd_split, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg Splitting fehlgeschlagen: {e.stderr}")
        raise

    # Generierte Segmente einsammeln
    created_segments = []
    part_idx = 0
    while True:
        seg_candidate = os.path.join(config.WORK_DIR, f"{base_name}_part{part_idx:02d}{ext}")
        if os.path.exists(seg_candidate):
            created_segments.append(seg_candidate)
            part_idx += 1
        else:
            break

    if not created_segments:
        raise RuntimeError("FFmpeg hat keine Videosegmente erzeugt.")

    return created_segments


"""Orchestrierung: process_single_file() steuert eine einzelne Datei durch
Validierung, Metadaten-Extraktion, Splitting, Upload und Aufräumen."""

import logging
import os
import shutil

from yt_upload import config
from yt_upload.fileutils import (
    cleanup_work_files,
    clear_segment_progress,
    load_segment_progress,
    resolve_target_path,
    save_segment_progress,
)
from yt_upload.media import (
    extract_metadata_and_thumb,
    is_file_ready_and_valid,
    split_video_if_needed,
)
from yt_upload.text_utils import censor_text, sanitize_text
from yt_upload.youtube_api import upload_single_video

logger = logging.getLogger(__name__)


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
    logger.info(f"--- VERARBEITE DATEI: {filename} ---")

    # Vorab-Prüfung auf Integrität der Quelldatei
    if not is_file_ready_and_valid(file_path):
        logger.error(f"Datei unvollständig oder ungültig: {filename}. Verschiebe nach CORRUPT...")
        target_corrupt = resolve_target_path(config.CORRUPT_DIR, filename)
        shutil.move(file_path, target_corrupt)
        return

    work_path = resolve_target_path(config.WORK_DIR, filename)
    logger.info(f"Verschiebe nach WORK: {work_path}")
    shutil.move(file_path, work_path)

    meta = extract_metadata_and_thumb(work_path)

    # Zusammenführen von Argumenten und Container-Metadaten
    raw_title = (args.title if args and hasattr(args, 'title') and args.title else meta["title"]) or os.path.splitext(filename)[0]
    raw_desc = (args.description if args and hasattr(args, 'description') and args.description else meta["description"]) or config.DEFAULT_DESCRIPTION

    raw_desc = sanitize_text(raw_desc)

    if meta.get("purl") and meta["purl"] not in raw_desc:
        raw_desc = f"{raw_desc}\n\nQuelle: {meta['purl']}"

    # Zensur-Filter anwenden
    title_base = censor_text(raw_title)
    desc_base = censor_text(raw_desc)

    category = (args.category if args and hasattr(args, 'category') and args.category else meta["genre"]) or config.DEFAULT_CATEGORY
    tags = (args.tags if args and hasattr(args, 'tags') and args.tags else meta["genre"]) or config.DEFAULT_TAGS
    rec_date = (args.recording_date if args and hasattr(args, 'recording_date') and args.recording_date else meta["date"])
    thumb_path = (args.thumbnail if args and hasattr(args, 'thumbnail') and args.thumbnail else meta["thumb_path"])

    privacy = (args.privacy if args and hasattr(args, 'privacy') and args.privacy else None) or config.VIDEO_PRIVACY
    publish_at = args.publish_at if args and hasattr(args, 'publish_at') else None
    license_type = (args.license if args and hasattr(args, 'license') and args.license else None) or "youtube"
    location = args.location if args and hasattr(args, 'location') else None
    default_lang = (args.default_language if args and hasattr(args, 'default_language') and args.default_language else None) or config.VIDEO_LANGUAGE
    default_audio_lang = (args.default_audio_language if args and hasattr(args, 'default_audio_language') and args.default_audio_language else None) or config.VIDEO_LANGUAGE
    embeddable = args.embeddable if (args and hasattr(args, 'embeddable') and args.embeddable is not None) else config.ALLOW_EMBEDDING
    
    target_playlist = config.PLAYLIST_NAME
    if args and hasattr(args, 'playlist') and args.playlist:
        target_playlist = args.playlist
    elif config.DYNAMIC_PLAYLISTS and meta["artist"]:
        target_playlist = meta["artist"]

    cred_file = (args.credentials_file if args and hasattr(args, 'credentials_file') and args.credentials_file else None) or config.CREDENTIALS_FILE
    client_secrets = args.client_secrets if args and hasattr(args, 'client_secrets') else None
    chunksize = args.chunksize if args and hasattr(args, 'chunksize') else 268435456
    open_link = args.open_link if args and hasattr(args, 'open_link') else False

    # Splitting-Prüfung ausführen
    segments = split_video_if_needed(work_path)

    # Fortschritt aus einem evtl. vorherigen fehlgeschlagenen Lauf laden (Segment-Dateiname -> Video-ID)
    progress = load_segment_progress(work_path)
    if progress:
        logger.info(f"Bestehender Fortschritt gefunden: {len(progress)} Segment(e) bereits hochgeladen, werden übersprungen.")

    # Segmente nacheinander hochladen
    for idx, seg in enumerate(segments):
        seg_key = os.path.basename(seg)

        if seg_key in progress:
            logger.info(f"Segment {seg_key} bereits hochgeladen (Video-ID {progress[seg_key]}), überspringe.")
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
        except Exception as e:  # noqa: BLE001 - Top-Level-Boundary für den gesamten Segment-Upload; Fehler aus Netzwerk, Dateisystem und API-Logik sollen hier gleichermaßen zu einem sauberen Retry/Corrupt-Handling führen statt den Daemon abstürzen zu lassen
            logger.error(f"Upload-Fehler bei Segment {seg}: {e}")

            if progress:
                # Mind. ein Segment wurde bereits erfolgreich hochgeladen: NICHT nach CORRUPT verschieben,
                # sonst gehen Original + Fortschritt-Zuordnung verloren und bereits hochgeladene Segmente
                # würden bei einem erneuten Lauf ein zweites Mal hochgeladen.
                target_retry = resolve_target_path(config.RETRY_DIR, filename)
                logger.warning(
                    f"{len(progress)} von {len(segments)} Segment(en) bereits erfolgreich hochgeladen. "
                    f"Verschiebe Original nach RETRY statt CORRUPT, Fortschritt bleibt erhalten: {target_retry}"
                )
                shutil.move(work_path, target_retry)
                # Nur die noch nicht hochgeladenen Segmente lokal aufräumen; bereits hochgeladene
                # Segment-Dateien können ebenfalls entfernt werden, da das Video schon bei YouTube liegt.
                cleanup_work_files(segments, is_error=True)
            else:
                target_corrupt = resolve_target_path(config.CORRUPT_DIR, filename)
                shutil.move(work_path, target_corrupt)
                cleanup_work_files(segments, is_error=True)
                clear_segment_progress(work_path)
            return

    # Bei Erfolg ins DONE-Verzeichnis verschieben
    target_done = resolve_target_path(config.DONE_DIR, filename)
    logger.info(f"Verarbeitung erfolgreich. Verschiebe Original nach DONE: {target_done}")
    shutil.move(work_path, target_done)
    cleanup_work_files(segments)
    clear_segment_progress(work_path)

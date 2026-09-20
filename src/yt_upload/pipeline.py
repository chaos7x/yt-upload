"""Orchestrierung: process_single_file() steuert eine einzelne Datei durch
Validierung, Metadaten-Extraktion, Splitting, Upload und Aufräumen."""

import contextlib
import logging
import os
import shutil
import tempfile

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
from yt_upload.youtube_api import QuotaExceededError, upload_single_video

logger = logging.getLogger(__name__)


def _quarantine_symlink(file_path, filename):
    """
    Verschiebt einen abgelehnten Symlink nach CORRUPT_DIR, OHNE ihn dabei
    jemals aufzulösen. shutil.move() würde bei einem Dateisystemwechsel
    zwischen IN_DIR und CORRUPT_DIR auf shutil.copy2() zurückfallen, das
    Symlinks standardmäßig dereferenziert (den Inhalt des Linkziels kopiert)
    - genau das Risiko, das process_single_file() mit der vorgelagerten
    is_symlink()-Prüfung eigentlich verhindern soll. os.rename() bewegt den
    Symlink selbst; schlägt das fehl (anderes Dateisystem), wird der Link am
    Ziel identisch nachgebildet statt aufgelöst.
    """
    target_corrupt = resolve_target_path(config.CORRUPT_DIR, filename)
    try:
        os.rename(file_path, target_corrupt)
    except OSError:
        os.symlink(os.readlink(file_path), target_corrupt)
        os.unlink(file_path)
    return target_corrupt


def process_single_file(file_path, args=None, manage_files=True):
    """
    Steuert die vollständige Verarbeitung einer Datei:
    1. Validierung
    2. Verschieben nach WORK (nur wenn manage_files=True)
    3. Metadaten-Extraktion & Zensierung
    4. Ggf. Splitting bei Überlänge
    5. Upload aller Teile
    6. Verschieben nach DONE (oder CORRUPT/RETRY bei Fehler; nur wenn manage_files=True)

    manage_files=True (Standard, von Auto-Batch und Dämon verwendet) verwaltet
    die Datei komplett über die festen WORK_DIR/DONE_DIR/CORRUPT_DIR/RETRY_DIR-
    Verzeichnisse, wie es für einen unbeaufsichtigten Lauf nötig ist. Der
    manuelle CLI-Modus (main.py, explizite Datei-Argumente) übergibt
    manage_files=False: die Quelldatei bleibt exakt dort liegen, wo der
    Nutzer sie angegeben hat - ein anwesender Mensch sieht den Erfolg/Fehler
    ohnehin direkt an Exit-Code/Log, ein automatisches Verschieben in eines
    dieser Verzeichnisse wäre nur überraschendes Verhalten. Ein eventuelles
    Splitting bei Überlänge (>10h) schreibt seine Segmente in diesem Fall in
    ein per tempfile.mkdtemp() erzeugtes Temp-Verzeichnis statt nach WORK_DIR.
    """
    filename = os.path.basename(file_path)
    logger.info(f"--- VERARBEITE DATEI: {filename} ---")

    # Symlinks ablehnen, BEVOR irgendeine Funktion die Datei öffnet
    # (is_file_ready_and_valid() ruft ffprobe auf, das dem Link transparent
    # folgen würde). Ein Symlink mit erlaubter Endung (z.B. "video.mp4" ->
    # eine beliebige lesbare Datei) in IN_DIR wäre sonst ein Primitive dafür,
    # beliebigen Dateiinhalt öffentlich zu YouTube hochzuladen.
    if os.path.islink(file_path):
        if manage_files:
            logger.warning(f"⚠️ Symlink wird nicht verarbeitet (Sicherheitsrisiko): {filename}. Verschiebe nach CORRUPT...")
            _quarantine_symlink(file_path, filename)
        else:
            logger.warning(f"⚠️ Symlink wird nicht verarbeitet (Sicherheitsrisiko): {filename}.")
        return

    # Vorab-Prüfung auf Integrität der Quelldatei
    if not is_file_ready_and_valid(file_path):
        if manage_files:
            logger.error(f"Datei unvollständig oder ungültig: {filename}. Verschiebe nach CORRUPT...")
            target_corrupt = resolve_target_path(config.CORRUPT_DIR, filename)
            shutil.move(file_path, target_corrupt)
        else:
            logger.error(f"Datei unvollständig oder ungültig: {filename}.")
        return

    split_temp_dir = None
    split_output_dir = None
    if manage_files:
        work_path = resolve_target_path(config.WORK_DIR, filename)
        logger.info(f"Verschiebe nach WORK: {work_path}")
        shutil.move(file_path, work_path)
    else:
        work_path = file_path
        # Nur bei tatsächlichem Splitting benötigt; wird unten wieder entfernt,
        # falls das Video die Maximallänge gar nicht überschreitet.
        split_temp_dir = tempfile.mkdtemp(prefix="yt-upload-split-")
        split_output_dir = split_temp_dir

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
    segments = split_video_if_needed(work_path, output_dir=split_output_dir)
    was_split = segments != [work_path]

    if split_temp_dir and not was_split:
        # Kein Splitting nötig gewesen - leeres Temp-Verzeichnis wieder entfernen.
        with contextlib.suppress(OSError):
            os.rmdir(split_temp_dir)
        split_temp_dir = None

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

            if not manage_files:
                # Datei bleibt unangetastet liegen - Fortschritt (falls vorhanden) ist
                # bereits als Sidecar-JSON neben der Originaldatei gespeichert, ein
                # erneuter Aufruf mit derselben Datei setzt automatisch dort fort.
                if progress:
                    logger.warning(
                        f"{len(progress)} von {len(segments)} Segment(en) bereits erfolgreich hochgeladen. "
                        f"Datei bleibt unverändert liegen: {file_path}. Erneuter Aufruf setzt fort."
                    )
                else:
                    logger.error(f"Kein Segment erfolgreich hochgeladen. Datei bleibt unverändert liegen: {file_path}.")
                    clear_segment_progress(work_path)
                if was_split:
                    cleanup_work_files(segments, is_error=True)
                if split_temp_dir:
                    with contextlib.suppress(OSError):
                        os.rmdir(split_temp_dir)
            elif progress:
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
            elif isinstance(e, QuotaExceededError):
                # Kein Problem mit der Datei selbst - ein taegliches API-/Kanal-Upload-
                # Kontingent ist aufgebraucht und loest sich von allein wieder auf (Reset
                # Mitternacht Pacific Time bzw. ~24h spaeter). Nach CORRUPT wuerde die Datei
                # faelschlich als dauerhaft fehlerhaft markiert und nie automatisch erneut
                # versucht.
                target_retry = resolve_target_path(config.RETRY_DIR, filename)
                logger.warning(f"Kontingent-Limit erreicht, verschiebe nach RETRY statt CORRUPT: {target_retry}")
                shutil.move(work_path, target_retry)
                cleanup_work_files(segments, is_error=True)
                clear_segment_progress(work_path)
            else:
                target_corrupt = resolve_target_path(config.CORRUPT_DIR, filename)
                shutil.move(work_path, target_corrupt)
                cleanup_work_files(segments, is_error=True)
                clear_segment_progress(work_path)
            return

    if not manage_files:
        logger.info(f"Verarbeitung erfolgreich. Datei bleibt unverändert liegen: {file_path}")
        if was_split:
            cleanup_work_files(segments)
        if split_temp_dir:
            with contextlib.suppress(OSError):
                os.rmdir(split_temp_dir)
    else:
        # Bei Erfolg ins DONE-Verzeichnis verschieben
        target_done = resolve_target_path(config.DONE_DIR, filename)
        logger.info(f"Verarbeitung erfolgreich. Verschiebe Original nach DONE: {target_done}")
        shutil.move(work_path, target_done)
        cleanup_work_files(segments)
    clear_segment_progress(work_path)

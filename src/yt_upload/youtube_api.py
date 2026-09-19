"""
YouTube Data API v3 Integration: OAuth-Token-Refresh, Resumable Upload (rohe
REST-API, kein google-api-python-client) und Playlist-Zuweisung.
"""

import json
import logging
import os
import sys
import time
import webbrowser

import requests

from yt_upload import config
from yt_upload.fileutils import cleanup_generated_thumbnail
from yt_upload.healthcheck import write_heartbeat
from yt_upload.text_utils import (
    get_valid_category_id,
    normalize_recording_date,
    parse_location,
    sanitize_text,
    truncate_title,
)

logger = logging.getLogger(__name__)


def get_access_token(cred_file=None, client_secrets_file=None):
    """
    Generiert mittels Refresh-Token einen frischen OAuth2 Access Token bei Google.
    Liest dazu die Client-Credentials aus der JSON-Datei ein.
    """
    target_cred = cred_file or config.CREDENTIALS_FILE
    if not os.path.exists(target_cred):
        raise FileNotFoundError(f"Credentials-Datei nicht gefunden: {target_cred}")

    # Sicherheits-Check: Datei enthält Client-Secret & Refresh-Token im Klartext,
    # daher sollte sie nicht für Gruppe/Andere lesbar sein.
    try:
        current_mode = os.stat(target_cred).st_mode
        if current_mode & 0o077:
            try:
                os.chmod(target_cred, 0o600)
                logger.warning(f"Credentials-Datei {target_cred} war zu offen berechtigt, auf 600 korrigiert.")
            except OSError as chmod_err:
                logger.warning(f"Credentials-Datei {target_cred} ist zu offen berechtigt und konnte nicht korrigiert werden: {chmod_err}")
    except OSError:
        pass

    with open(target_cred, encoding="utf-8") as f:
        data = json.load(f)

    client_data = data
    if client_secrets_file:
        with open(client_secrets_file, encoding="utf-8") as f:
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


class PermanentUploadError(RuntimeError):
    """Wird bei dauerhaften (nicht behebbaren) API-Fehlern ausgelöst, um sinnloses Retrying zu vermeiden."""


class _UploadProgressBar:
    """
    Live-Fortschrittsbalken auf stderr fuer interaktive Uploads (Vorbild:
    tokland/youtube-upload's progressbar2-Widget aus Percentage/Bar/
    FileTransferSpeed/DataSize/ETA). Bewusst ohne die zusaetzliche
    progressbar2-Dependency von Hand nachgebaut - dieses Projekt haelt sich
    schon bei inotify/requests bewusst an minimale, per apt/apk statt PyPI
    installierbare Abhaengigkeiten, und fuer eine reine Terminal-Optik lohnt
    sich das Docker-Paketierungs-Gedoens (3 Dockerfile-Varianten + Bare-Metal)
    nicht. Aktualisiert sich nur bei Chunk-Grenzen (siehe --chunksize) - fuer
    einen glatteren Balken kleinere Chunksize waehlen.
    """

    BAR_WIDTH = 30

    def __init__(self, total_bytes):
        self.total_bytes = total_bytes
        self.start_time = time.monotonic()

    def update(self, uploaded_bytes):
        elapsed = max(time.monotonic() - self.start_time, 0.001)
        speed = uploaded_bytes / elapsed
        pct = min(uploaded_bytes / self.total_bytes, 1.0) if self.total_bytes else 1.0
        filled = int(self.BAR_WIDTH * pct)
        bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
        remaining_bytes = max(self.total_bytes - uploaded_bytes, 0)
        eta_sec = int(remaining_bytes / speed) if speed > 0 else 0
        eta = time.strftime("%H:%M:%S", time.gmtime(eta_sec))
        sys.stderr.write(
            f"\r[{bar}] {pct * 100:5.1f}% "
            f"{speed / (1024 * 1024):6.2f} MB/s "
            f"{uploaded_bytes / (1024 * 1024):8.1f}/{self.total_bytes / (1024 * 1024):.1f} MB "
            f"ETA {eta}"
        )
        sys.stderr.flush()

    def finish(self):
        self.update(self.total_bytes)
        sys.stderr.write("\n")
        sys.stderr.flush()


def _probe_upload_offset(session, upload_url, access_token, file_size, timeout=60):
    """
    Fragt bei einem unklaren Chunk-Fehler (z.B. "Failed to parse Content-Range
    header" - ein bekannter, gelegentlich transienter Ausrutscher der YouTube-
    API, kein echter Client-Bug) den tatsächlichen Stand der Resumable-Upload-
    Session bei Google ab, statt blind erneut zu senden oder sofort
    aufzugeben: leerer PUT-Body mit `Content-Range: bytes */{file_size}` ist
    laut Google-Doku das vorgesehene Verfahren, um den echten Server-Offset
    zu erfragen. Gibt (video_id, uploaded_bytes) zurück - video_id ist None,
    solange der Upload laut Server noch nicht abgeschlossen ist.
    """
    probe_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Range": f"bytes */{file_size}",
        "Content-Length": "0",
    }
    probe_res = session.put(upload_url, headers=probe_headers, data=b"", timeout=timeout)

    if probe_res.status_code in (200, 201):
        return probe_res.json().get("id"), file_size

    if probe_res.status_code == 308:
        range_hdr = probe_res.headers.get("Range")
        if range_hdr and "-" in range_hdr:
            try:
                return None, int(range_hdr.split("-")[1]) + 1
            except (ValueError, IndexError):
                pass
        return None, 0

    raise RuntimeError(f"Status-Check fehlgeschlagen ({probe_res.status_code}): {probe_res.text[:500]}")


def add_video_to_playlist(video_id, playlist_name, access_token, privacy=config.VIDEO_PRIVACY,
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
        except (OSError, ValueError, KeyError, requests.exceptions.RequestException) as e:
            logger.warning(f"Token-Refresh für Playlist-Zuweisung fehlgeschlagen: {e}")
            return False

    try:
        playlist_id = None
        next_page = None
        auth_retry_used = False

        # Durchsuche bestehende Playlists des Nutzers
        while True:
            list_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50"
            if next_page:
                list_url += f"&pageToken={next_page}"

            res = requests.get(list_url, headers=headers, timeout=30)

            if res.status_code in (401, 403) and not auth_retry_used:
                logger.warning("Playlist-Abruf: Token abgelaufen, erneuere und versuche erneut...")
                auth_retry_used = True
                if _refresh_token_if_possible():
                    continue
                logger.warning(f"Konnte Playlists nicht abrufen ({res.status_code}): {res.text}")
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
                logger.warning(f"Konnte Playlists nicht abrufen ({res.status_code}): {res.text}")
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
                logger.warning("Playlist-Erstellung: Token abgelaufen, erneuere und versuche erneut...")
                auth_retry_used = True
                if _refresh_token_if_possible():
                    create_res = requests.post(create_url, headers=headers, json=create_body, timeout=30)
            if create_res.status_code in (200, 201):
                playlist_id = create_res.json().get("id")
            elif create_res.status_code not in (200, 201):
                logger.warning(f"Konnte Playlist nicht erstellen ({create_res.status_code}): {create_res.text}")

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
                    logger.warning("Playlist-Zuweisung: Token abgelaufen, erneuere und versuche erneut...")
                    auth_retry_used = True
                    if _refresh_token_if_possible():
                        continue

                if item_res.status_code in (200, 201):
                    logger.info(f"Video {video_id} erfolgreich zur Playlist '{playlist_name}' hinzugefügt.")
                    return True

                if item_res.status_code in (409, 500, 502, 503, 504) and attempt < max_retries:
                    logger.warning(
                        f"YouTube API meldet {item_res.status_code} beim Playlist-Assignment. "
                        f"Retry {attempt}/{max_retries} in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.warning(f"Video konnte Playlist nicht hinzugefügt werden: {item_res.text}")
                    break

    except (requests.exceptions.RequestException, ValueError, KeyError, OSError) as e:
        logger.error(f"Fehler bei Playlist-API: {e}")
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
    privacy=config.VIDEO_PRIVACY,
    publish_at=None,
    license_type="youtube",
    location=None,
    default_lang=config.VIDEO_LANGUAGE,
    default_audio_lang=config.VIDEO_LANGUAGE,
    embeddable=config.ALLOW_EMBEDDING,
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
        logger.info(f"Status wurde für geplanten Upload von '{privacy}' auf 'private' korrigiert.")
        privacy = "private"

    logger.info(f"Lade hoch via native HTTP REST API ({privacy}): {os.path.basename(file_path)}")

    file_size = os.path.getsize(file_path)
    access_token = get_access_token(cred_file, client_secrets_file)

    session = requests.Session()

    # Ausrichtung der Chunksize an die von YouTube vorgeschriebene 256-KiB-Grenze
    if chunksize % config.CHUNK_UNIT_BYTES != 0:
        adjusted_chunksize = max(config.CHUNK_UNIT_BYTES, (chunksize // config.CHUNK_UNIT_BYTES) * config.CHUNK_UNIT_BYTES)
        logger.info(f"Chunksize angepasst auf Vielfaches von 256 KiB: {chunksize} -> {adjusted_chunksize} Bytes")
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

    logger.debug(f"PAYLOAD DEBUG: {json.dumps(metadata_body, ensure_ascii=False)}")

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

    logger.info("Initialisiere Resumable Upload Session...")

    init_res = None
    init_max_retries = 3
    for init_attempt in range(1, init_max_retries + 1):
        try:
            init_res = session.post(init_url, headers=init_headers, json=metadata_body, timeout=60)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Netzwerkfehler bei Session-Init (Versuch {init_attempt}/{init_max_retries}): {e}")
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
            logger.warning(
                f"YouTube API meldet {init_res.status_code} bei Session-Init. "
                f"Retry {init_attempt}/{init_max_retries} in {2 ** init_attempt}s..."
            )
            time.sleep(2 ** init_attempt)
            continue

        raise RuntimeError(f"Session-Init fehlgeschlagen ({init_res.status_code}): {init_res.text}")

    upload_url = init_res.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Keine Upload-Location im Header erhalten.")

    logger.info(f"Starte Chunk-Upload ({file_size / (1024 * 1024):.2f} MB) mit Chunksize {chunksize / (1024 * 1024):.1f} MB...")

    max_retries = 5
    video_id = None
    uploaded_bytes = 0
    progress_bar = _UploadProgressBar(file_size) if sys.stderr.isatty() else None

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
                        write_heartbeat()
                        if progress_bar:
                            progress_bar.finish()
                        logger.info(f"Upload ERFOLGREICH abgeschlossen! Video-ID: {video_id}")
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

                        if uploaded_bytes < file_size and uploaded_bytes % config.CHUNK_UNIT_BYTES != 0:
                            uploaded_bytes = (uploaded_bytes // config.CHUNK_UNIT_BYTES) * config.CHUNK_UNIT_BYTES
                            logger.warning(f"Offset korrigiert auf 256-KiB-Grenze: {uploaded_bytes} Bytes")

                        # Fortschritt IMMER auch als Log-Zeile ausgeben, nicht nur wenn kein
                        # Live-Balken aktiv ist: sys.stderr.isatty() liefert in Containern mit
                        # `tty: true` (z.B. fuer Podman-Kompatibilitaet gesetzt) auch im
                        # unbeaufsichtigten Daemon-Betrieb True, wodurch bislang ausschliesslich
                        # der Live-Balken lief - der schreibt per \r direkt auf stderr, landet
                        # also nie im Logger/in `docker logs`-Historie, und macht den Fortschritt
                        # bei einem spaeteren Blick ins Log unsichtbar.
                        pct = (uploaded_bytes / file_size) * 100
                        logger.info(f"Fortschritt: {uploaded_bytes / (1024 * 1024):.1f} / {file_size / (1024 * 1024):.1f} MB ({pct:.1f}%)")
                        if progress_bar:
                            progress_bar.update(uploaded_bytes)
                        chunk_success = True
                        write_heartbeat()
                        break

                    # Token abgelaufen: Access Token erneuern und erneut versuchen
                    elif put_res.status_code in (401, 403):
                        access_token = get_access_token(cred_file, client_secrets_file)
                        chunk_headers["Authorization"] = f"Bearer {access_token}"
                        raise RuntimeError("Token erneuert, versuche Chunk erneut...")

                    # Alle übrigen Status-Codes: immer loggen, damit Fehler nicht stillschweigend verschwinden
                    else:
                        logger.error(
                            f"Unerwarteter Status {put_res.status_code} beim Chunk-Upload "
                            f"(Versuch {attempt}/{max_retries}): {put_res.text[:500]}"
                        )

                        # "Failed to parse Content-Range header": bekannter, gelegentlich
                        # transienter Ausrutscher der YouTube-API (v.a. beim letzten Chunk
                        # sehr grosser Uploads), kein echter Client-Bug - statt das wie einen
                        # dauerhaften Fehler zu behandeln und die ganze Datei zu verwerfen,
                        # den tatsaechlichen Serverstand per Status-Check abfragen und von
                        # dort weitermachen.
                        if put_res.status_code == 400 and "Content-Range" in put_res.text:
                            try:
                                probe_video_id, probe_uploaded = _probe_upload_offset(
                                    session, upload_url, access_token, file_size
                                )
                            except (requests.exceptions.RequestException, RuntimeError) as probe_err:
                                logger.warning(f"Status-Check nach Content-Range-Fehler fehlgeschlagen: {probe_err}")
                            else:
                                if probe_video_id:
                                    video_id = probe_video_id
                                    uploaded_bytes = file_size
                                    chunk_success = True
                                    write_heartbeat()
                                    if progress_bar:
                                        progress_bar.finish()
                                    logger.info(
                                        f"Upload war laut Status-Check bereits abgeschlossen! Video-ID: {video_id}"
                                    )
                                    break

                                uploaded_bytes = probe_uploaded
                                if uploaded_bytes < file_size and uploaded_bytes % config.CHUNK_UNIT_BYTES != 0:
                                    uploaded_bytes = (uploaded_bytes // config.CHUNK_UNIT_BYTES) * config.CHUNK_UNIT_BYTES
                                logger.warning(
                                    "Content-Range-Fehler war vermutlich transient - Serverstand laut "
                                    f"Status-Check: {uploaded_bytes / (1024 * 1024):.1f} MB. Setze fort..."
                                )
                                chunk_success = True
                                write_heartbeat()
                                break

                        # Dauerhafte Client-Fehler (z.B. 400 ungültige Metadaten, 404 Session weg)
                        # lassen sich durch Wiederholen nicht beheben -> sofort abbrechen statt 5x zu retryen
                        if 400 <= put_res.status_code < 500 and put_res.status_code not in (401, 403, 408, 429):
                            raise PermanentUploadError(
                                f"Nicht behebbarer Fehler ({put_res.status_code}): {put_res.text[:500]}"
                            )
                        raise RuntimeError(f"Transienter Fehler ({put_res.status_code}), versuche erneut...")

                except PermanentUploadError:
                    raise  # nicht abfangen/retryen - direkt an den Aufrufer durchreichen

                except Exception as e:  # noqa: BLE001 - fängt hier bewusst auch selbst geworfene RuntimeErrors als Retry-Signal ab, nicht nur echte Netzwerkfehler
                    logger.warning(f"Fehler bei Chunk-Upload (Versuch {attempt}/{max_retries}): {e}")
                    time.sleep(2 ** attempt)

            if not chunk_success:
                raise RuntimeError("Max Retries beim Upload überschritten.")

    # 3. Post-Upload-Schritte: Thumbnail hochladen und Playlist-Zuweisung
    # Bei sehr großen Dateien kann der Chunk-Upload allein schon die Token-Lebensdauer
    # (~1h) überschreiten. Token hier proaktiv erneuern statt erst bei 401 zu reagieren.
    try:
        access_token = get_access_token(cred_file, client_secrets_file)
    except (OSError, ValueError, KeyError, requests.exceptions.RequestException) as refresh_err:
        logger.warning(f"Token-Refresh vor Post-Upload-Schritten fehlgeschlagen, verwende bestehenden Token: {refresh_err}")

    try:
        if video_id:
            if thumb_path and os.path.exists(thumb_path):
                try:
                    logger.info(f"Lade benutzerdefiniertes Thumbnail hoch: {thumb_path}")
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
                            logger.info("Thumbnail erfolgreich gesetzt.")
                            break
                        elif t_res.status_code in (401, 403) and thumb_attempt == 1:
                            logger.warning("Thumbnail-Upload: Token abgelaufen, erneuere und versuche erneut...")
                            access_token = get_access_token(cred_file, client_secrets_file)
                            continue
                        else:
                            logger.warning(f"Thumbnail-Upload fehlgeschlagen ({t_res.status_code}): {t_res.text}")
                            break
                except (OSError, ValueError, KeyError, requests.exceptions.RequestException) as te:
                    logger.warning(f"Fehler beim Thumbnail-Setzen: {te}")

            if playlist_name:
                add_video_to_playlist(
                    video_id, playlist_name, access_token, privacy,
                    cred_file=cred_file, client_secrets_file=client_secrets_file
                )

            if open_link:
                v_url = f"https://www.youtube.com/watch?v={video_id}"
                logger.info(f"Öffne Browser-Link: {v_url}")
                webbrowser.open(v_url)
    finally:
        cleanup_generated_thumbnail(thumb_path)

    return video_id



"""
Konfiguration & Standard-Pfade.

Enthält alle Laufzeit-Einstellungen als Modul-Globals sowie load_configuration()
für das Hot-Reload-Verhalten. Andere Module referenzieren diese Werte IMMER als
`config.NAME` (nie `from yt_upload.config import NAME`), damit sie nach einem
erneuten load_configuration()-Aufruf den aktuellen Wert sehen statt einer beim
Import eingefrorenen Kopie.
"""

import configparser
import glob
import hashlib
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# ==============================================================================
# KONFIGURATION & STANDARD-PFADE
# Pfade werden primär aus Umgebungsvariablen bezogen oder auf Default-Werte gesetzt.
# ==============================================================================
CONF_PATH = os.environ.get('CONFIG_FILE', '/etc/yt-upload/upload.conf')
CONF_D_DIR = os.environ.get('CONF_D_DIR', os.path.join(os.path.dirname(CONF_PATH), 'conf.d'))
# BASE_DIR zeigt auf das Package-Verzeichnis (yt_upload/); für den bisherigen
# Docker-Log-Fallback (BASE_DIR/upload.log) ist das unkritisch, da dieser Fall
# ohnehin nur bare-metal ohne /log und ohne /var/log-Schreibrechte greift.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# YouTube Limitierungen & Netzwerk-Chunk-Spezifikationen
SEGMENT_TIME_SEC = 36000     # Maximum 10 Stunden pro Video vor automatischem Splitting
CHUNK_UNIT_BYTES = 262144    # 256 KiB Basis-Einheit für Resumable Chunk Uploads (Zwingende YouTube API Vorgabe)


def _is_dedicated_mount(path):
    """
    Prüft, ob path ein eigener Mountpoint ist (Docker-Volume/Bind-Mount) statt
    nur ein gewöhnliches Verzeichnis, das das Dockerfile per `mkdir -p` fest
    ins Image gebacken hat (z.B. /log, /videos) - eine reine Existenzprüfung
    kann diese beiden Fälle nicht unterscheiden, da `mkdir -p` das Verzeichnis
    auch ganz ohne jeden Mount anlegt (siehe Bug: /log-Logdatei wurde erzeugt,
    obwohl kein Log-Volume mehr gemountet war). Vergleicht dazu die
    Geräte-ID (st_dev) von path und seinem Elternverzeichnis: unterschiedliche
    st_dev bedeutet, dass dort tatsächlich ein Volume/Bind-Mount eingehängt ist.
    """
    if not os.path.isdir(path):
        return False
    parent = os.path.dirname(path.rstrip("/")) or "/"
    try:
        return os.stat(path).st_dev != os.stat(parent).st_dev
    except OSError:
        return False


# Alle fuenf Verzeichnisse zeigen einheitlich (Docker wie Bare-Metal) in
# denselben /srv/media-pipeline-Namespace statt der inzwischen abgeloesten
# /videos/*-Mountpunkte - IN_DIR (geteilt mit fetchbridge) bleibt dabei bewusst
# im selben Namespace wie WORK_DIR/DONE_DIR/CORRUPT_DIR/RETRY_DIR (nur intern
# von yt-upload genutzt), da ein gemeinsamer Ort fuer saemtliche Pipeline-Daten
# einfacher zu ueberblicken ist als zwei verschiedene Namespaces mit
# unterschiedlicher Herkunft. Das .deb-Postinst legt alle fuenf mit derselben
# gemeinsamen Gruppe an; dass WORK_DIR & Co. damit auch fuer tw-recorder/
# fetchbridge technisch zugreifbar waeren, ist hier bewusst in Kauf genommen
# (keiner der beiden liest/schreibt dort tatsaechlich).
IN_DIR = os.environ.get('IN_DIR', "/srv/media-pipeline/incoming")
WORK_DIR = os.environ.get('WORK_DIR', "/srv/media-pipeline/work")
DONE_DIR = os.environ.get('DONE_DIR', "/srv/media-pipeline/done")
CORRUPT_DIR = os.environ.get('CORRUPT_DIR', "/srv/media-pipeline/corrupt")
# Für Dateien, bei denen bereits mind. ein Segment erfolgreich hochgeladen wurde, bevor ein Fehler auftrat.
# Getrennt von CORRUPT_DIR, damit kein versehentlicher Doppel-Upload bereits hochgeladener Segmente droht.
RETRY_DIR = os.environ.get('RETRY_DIR', "/srv/media-pipeline/retry")

DEBUG_MODE = os.environ.get('DEBUG', '').lower() in ('true', 'yes', '1')
os.environ['DEBUG'] = '1' if DEBUG_MODE else '0'

_VALID_LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')


def _resolve_log_level_name(debug_mode: bool) -> str:
    """
    Ermittelt den LOG_LEVEL-Namen nach Priorität:
    1. LOG_LEVEL (DEBUG/INFO/WARNING/ERROR/CRITICAL, case-insensitive) - die
       flexible Variante, einheitlich mit tw-recorder/fetchbridge.
    2. debug_mode (aus DEBUG=true/yes/1) - reine Kurzform für
       LOG_LEVEL=DEBUG, nur als Default falls LOG_LEVEL NICHT gesetzt ist.
    3. Fallback INFO, auch bei einem ungültigen LOG_LEVEL-Wert (kein
       AttributeError wegen eines Tippfehlers in der ENV).
    """
    default_name = 'DEBUG' if debug_mode else 'INFO'
    name = os.environ.get('LOG_LEVEL', '').strip().upper() or default_name
    return name if name in _VALID_LOG_LEVELS else default_name


LOG_LEVEL_NAME = _resolve_log_level_name(DEBUG_MODE)


def _default_log_file():
    """
    Ermittelt den Standard-Logpfad:
    1. /log/upload.log, falls /log als eigenes Docker-Volume gemountet ist
       (nicht nur als vom Dockerfile angelegtes Verzeichnis vorhanden, siehe
       _is_dedicated_mount())
    2. /var/log/yt-upload/yt-upload.log, falls beschreibbar (FHS-Standard für Bare-Metal-Daemons)
    3. BASE_DIR/upload.log als letzter Fallback (z.B. lokales Testen ohne Root-Rechte)
    """
    if _is_dedicated_mount("/log"):
        return "/log/upload.log"

    var_log_dir = "/var/log/yt-upload"
    try:
        os.makedirs(var_log_dir, exist_ok=True)
        if os.access(var_log_dir, os.W_OK):
            return os.path.join(var_log_dir, "yt-upload.log")
    except OSError:
        pass

    return os.path.join(BASE_DIR, "upload.log")


_env_log_file_override = os.environ.get('LOG_FILE', '').strip()
LOG_FILE = _env_log_file_override or _default_log_file()
# Merkt sich, ob LOG_FILE explizit (ENV oder später Config) gesetzt wurde,
# im Unterschied zur automatischen Pfad-Ermittlung via _default_log_file().
# Wird von logging_setup.setup_logging() genutzt, um den Datei-Handler auch
# ohne Syslog-Daemon zu aktivieren, wenn der Pfad ausdrücklich konfiguriert wurde.
LOG_FILE_EXPLICIT = bool(_env_log_file_override)


def _resolve_credentials_file(in_container: bool) -> str:
    """
    Bare-Metal-Default ist /etc/yt-upload/ (derselbe Ort wie upload.conf),
    nicht BASE_DIR (Installationsort des Python-Packages, z.B.
    site-packages) - dort war die Datei für den dedizierten
    yt-upload-Systemuser weder zuverlässig auffindbar noch beschreibbar.
    get_token.py (get-token) nutzt denselben Default (siehe dortiger
    Kommentar), damit beide Tools ohne manuelle ENV-Konfiguration dieselbe
    Datei meinen.
    """
    default = "/app/oauth/youtube-upload-credentials.json" if in_container else "/etc/yt-upload/youtube-upload-credentials.json"
    return os.environ.get('CREDENTIALS_FILE', default)


CREDENTIALS_FILE = _resolve_credentials_file(os.path.exists("/app/oauth"))

# Heartbeat-Datei für den Healthcheck (z.B. Docker HEALTHCHECK). Der Dämon
# aktualisiert sie regelmäßig; ein separater, sehr leichtgewichtiger Aufruf
# desselben Skripts (--healthcheck) prüft nur, ob sie frisch genug ist -
# ohne Config zu laden oder Verzeichnisse anzulegen, damit der Healthcheck
# selbst schnell ist und keine Nebenwirkungen hat.
HEALTH_FILE = os.environ.get('HEALTH_FILE', os.path.join(tempfile.gettempdir(), "yt-upload.health"))
# Wie alt die Heartbeat-Datei maximal sein darf, bevor --healthcheck als
# "unhealthy" (Exit-Code 1) gilt. Grosszügig bemessen, da ein einzelner
# Upload-Chunk bei langsamer Verbindung durchaus mehrere Minuten dauern kann.
HEALTH_STALE_SECONDS = int(os.environ.get('HEALTH_STALE_SECONDS', '300'))

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
    global LOG_FILE_EXPLICIT

    in_dir = IN_DIR
    work_dir = WORK_DIR
    done_dir = DONE_DIR
    corrupt_dir = CORRUPT_DIR
    retry_dir = RETRY_DIR
    log_file = LOG_FILE
    log_file_explicit = LOG_FILE_EXPLICIT
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
            hasher.update(cfg.encode())
            hasher.update(str(os.path.getmtime(cfg)).encode())
            with open(cfg, 'rb') as f:
                hasher.update(f.read())
        except OSError:
            pass

    # Protokolliere Änderungen falls sich der Hash unterscheidet
    new_hash = hasher.hexdigest()
    if log_changes and CURRENT_CONFIG_HASH and new_hash != CURRENT_CONFIG_HASH:
        files_str = ", ".join(file_list_names) if file_list_names else "conf.d"
        logger.info(f"🔄 Konfigurationsänderung erkannt (geändert: {files_str}). Synchronisiere...")
    CURRENT_CONFIG_HASH = new_hash

    # --- Step 1: Config-Dateien verarbeiten ---
    # Bewusst einzeln statt config.read(config_files, ...) mit der ganzen
    # Liste auf einmal: ConfigParser.read() bricht beim ersten Parse-Fehler
    # in der Liste komplett ab, wodurch jede DANACH folgende Datei (auch eine
    # gültige!) stillschweigend gar nicht mehr gelesen wird - ein Tippfehler
    # in einer frühen conf.d-Datei würde sonst alle alphabetisch späteren
    # unsichtbar deaktivieren, ohne dass das aus der Warnung ersichtlich wäre.
    if config_files:
        for cfg_file in config_files:
            try:
                # config.read(cfg_file, ...) öffnet die Datei intern und
                # FÄNGT einen dabei auftretenden OSError (z.B. Permission
                # denied, falls eine conf.d-Datei versehentlich dem falschen
                # User/einer falschen Gruppe gehört) STILL AB, ohne ihn je an
                # aufrufenden Code durchzureichen - das except unten würde so
                # einen Fall nie sehen, die Datei würde ohne jede Warnung
                # einfach ignoriert. Mit dem eigenen open() landet ein
                # Berechtigungsfehler dagegen in unserem eigenen except.
                with open(cfg_file, encoding='utf-8') as fp:
                    config.read_file(fp, source=cfg_file)
            except (OSError, configparser.Error, UnicodeDecodeError) as e:
                # Fail-fast statt eines uncaught ParsingError, der den ganzen
                # Dämon abstürzen ließe (z.B. bei einer Zeile ohne "=", einem
                # doppelten Abschnitt oder einer kaputten Zeichenkodierung) -
                # die untenstehenden config.get(fallback=...)-Aufrufe bleiben
                # dann einfach bei ihren aktuellen Werten (Alt-Config bzw.
                # Hardcoded-Defaults beim allerersten Laden), nur diese eine
                # Datei wird übersprungen.
                logger.warning(f"Fehler beim Lesen von {cfg_file}: {e}")

        # Sektion [paths] einlesen
        if 'paths' in config:
            in_dir = config.get('paths', 'in_dir', fallback=in_dir)
            work_dir = config.get('paths', 'work_dir', fallback=work_dir)
            done_dir = config.get('paths', 'done_dir', fallback=done_dir)
            corrupt_dir = config.get('paths', 'corrupt_dir', fallback=corrupt_dir)
            retry_dir = config.get('paths', 'retry_dir', fallback=retry_dir)
            if config.has_option('paths', 'log_file'):
                log_file_explicit = True
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
    if 'LOG_FILE' in os.environ:
        log_file_explicit = True
    LOG_FILE = os.environ.get('LOG_FILE', log_file)
    LOG_FILE_EXPLICIT = log_file_explicit
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

    DESCRIPTION_BLACKLIST = sorted(combined_blacklist)

    ALLOW_EMBEDDING = allow_embedding
    AUTO_GENERATE_THUMBNAIL = auto_generate_thumbnail
    AUTO_THUMB_MIN_SEC = auto_thumb_min_sec
    AUTO_THUMB_MAX_SEC = auto_thumb_max_sec
    ALLOW_OVERWRITE = allow_overwrite


# Initiales Laden beim Scriptstart ausführen
load_configuration(log_changes=False)

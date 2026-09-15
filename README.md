# yt-upload 📹☁️

`yt-upload` ist eine Python- und Docker-basierte Automatisierungslösung zur ereignisbasierten Überwachung von Video-Verzeichnissen und zum automatischen Upload auf YouTube via native REST API v3, FFmpeg und inotify.

Das Tool verarbeitet eingehende Videodateien, extrahiert eingebettete Metadaten sowie Cover-Thumbnails, zerlegt Videos bei Bedarf verlustfrei in Segmente (z. B. bei Dateien > 10 Stunden) und ordnet sie dynamisch Playlists zu.

---

## ✨ Features

* **Direkte HTTP REST API v3:** Native Implementierung für Resumable Chunk-Uploads ohne schwerfällige externe API-Wrapper.
* **Inotify-Ordnerüberwachung:** Überwacht `/videos/in` im Dämon-Modus (`-D`) in Echtzeit auf Dateiveränderungen (`.mp4`, `.mkv`, `.mov`, `.m4v`) inklusive Polling-Fallback, falls `inotify` nicht verfügbar ist.
* **Drei flexible Betriebsmodi:**
  1. **Dämon-Modus (`-D` / `--daemon`):** Dauerhafter Hintergrunddienst zur automatischen Überwachung.
  2. **Auto-Batch (`-a` / `--auto`):** Einmaliges Abarbeiten eines Verzeichnisses mit anschließendem Beenden.
  3. **Manuell (CLI):** Upload einzelner Videodateien mit voller Parameterkontrolle.
* **Metadaten- & Thumbnail-Extraktion:** Liest Titel, Beschreibung, PURL, Genre, Aufnahmedatum und Artist via `ffprobe` aus. Extrahiert automatisch Thumbnails aus MKV-Attachments, MP4-Covern oder generiert ein Frame-Thumbnail.
* **Verlustfreies FFmpeg-Splitting:** Zertrennt Videos mit einer Laufzeit von über 10 Stunden (36.000 Sekunden) automatisch und ohne Qualitätsverlust (`-c copy`) in durchnummerierte Segmente.
* **Dynamische Playlist-Verwaltung:** Erstellt und verknüpft Ziel-Playlists automatisch (z. B. auf Basis des `ARTIST`-Tags via `ENABLE_DYNAMIC_PLAYLISTS`).
* **Robustes Retry & Health-Check:** Prüft Videodateien vor dem Upload auf unvollständige Schreibvorgänge / fehlende `moov`-Atome. Bereits erfolgreich hochgeladene Segmente werden bei einem Fehler im nächsten Segment nicht erneut hochgeladen (Fortschritt wird pro Datei persistiert) - fehlgeschlagene Jobs mit Teilfortschritt landen in `videos/retry`, komplett fehlerhafte in `videos/corrupt`.

---

## 🚀 Schnellstart (Docker)

### 1. Ordnerstruktur anlegen
Erstelle die benötigten Ordnerstrukturen auf deinem Host-System:

```bash
mkdir -p videos/in videos/work videos/done videos/corrupt videos/retry log oauth etc/yt-upload/conf.d
```

### 2. OAuth-Secrets hinterlegen
Hinterlege deine Google OAuth Secrets unter `oauth/client_secrets.json`. Eine Beispielvorlage für die Datei:

```json
{
  "installed": {
    "client_id": "DEINE_CLIENT_ID.apps.googleusercontent.com",
    "project_id": "DEIN_PROJECT_ID",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_secret": "DEIN_CLIENT_SECRET",
    "redirect_uris": ["http://localhost"]
  }
}
```

### 3. OAuth-Token generieren
Führe das mitgelieferte Authentifizierungsskript im Container aus:

```bash
docker run --rm -it \
  -v $(pwd)/oauth:/app/oauth \
  ghcr.io/chaos7x/yt-upload:latest /usr/local/bin/get_token
```

### 4. Starten via Docker CLI
```bash
docker run -d \
  --name yt-upload \
  --hostname yt-upload \
  --restart unless-stopped \
  --security-opt no-new-privileges:true \
  -u "11107:11108" \
  -e TZ=Europe/Berlin \
  -e ENABLE_DYNAMIC_PLAYLISTS=true \
  -e HOME=/tmp \
  -v $(pwd)/oauth:/app/oauth:rw \
  -v $(pwd)/videos:/videos:rw \
  -v $(pwd)/log:/log:rw \
  ghcr.io/chaos7x/yt-upload:latest
```

### 5. 📦 Docker Compose Integration
```yaml
services:
  yt-upload:
    image: ghcr.io/chaos7x/yt-upload:${IMAGE_VERSION:-latest}
    container_name: yt-upload
    hostname: yt-upload
    restart: unless-stopped

    security_opt:
      - no-new-privileges:true

    user: "11107:11108"
    environment:
      - TZ=Europe/Berlin
      - ENABLE_DYNAMIC_PLAYLISTS=true
      - HOME=/tmp
      - ENABLE_DESCRIPTION_CENSOR=true
      - DESCRIPTION_BLACKLIST=onlyfans.com,fansly.com,loyalfans.com,manyvids.com,pornhub.com,chaturbate.com,stake.com,csgoroll.com,hellcase.com,1xbet.com,adf.ly,shorte.st
    env_file:
      - .env
    volumes:
      - oauth:/app/oauth
      - videos:/videos
      - log:/log
      # Optional: eigene upload.conf statt der im Image mitgelieferten
      # Beispielkonfiguration nutzen (einzelne Datei, kein ganzes Verzeichnis):
      # - ./upload.conf:/etc/yt-upload/upload.conf:ro

    tty: true
    stdin_open: true

volumes:
  oauth:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: "./oauth"

  videos:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: "./videos"

  log:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: "./log"
```

---

## 🛠️ Bare-Metal-Installation (ohne Docker)

`yt-upload` läuft auch direkt auf dem Host, ganz ohne Container. Systemabhängigkeiten kommen bewusst über den jeweiligen Paketmanager statt über PyPI - `pip` installiert ausschließlich das eigene Package:

```bash
# System-Abhängigkeiten (requests immer, inotify nur für den Dämon-Modus)
apt install python3-pip python3-setuptools python3-requests python3-inotify ffmpeg
# (unter Alpine/anderen Distros entsprechend: apk add / dnf install ...)

cd /pfad/zu/yt-upload
pip install --break-system-packages --no-deps .
```

Danach steht der Befehl `yt-upload` systemweit zur Verfügung (`yt-upload --version` zum Testen). Für ein Update genügt ein erneuter `pip install ...`-Aufruf im aktualisierten Repo-Verzeichnis.

Die Logdatei landet je nach Umgebung automatisch am sinnvollsten Ort (`/var/log/yt-upload/`, sofern beschreibbar und ein klassischer Syslog-Daemon läuft, sonst nur auf `stdout`/journald) - siehe `LOG_FILE`-Umgebungsvariable, falls ein fester Pfad gewünscht ist.

---

## 🐳 Docker-Image-Varianten

Es gibt drei Dockerfiles für unterschiedliche Basis-Images - alle bauen dasselbe `yt_upload`-Package ein, nur der Installationsweg passt sich an die jeweilige Distribution an:

| Dockerfile | Basis | Installationsweg |
|---|---|---|
| `Dockerfile` (Standard) | `debian:trixie-slim` | Zweistufiger Build, `pip install --no-deps` in einer separaten Builder-Stage, System-Pakete (`requests`) über `apt` |
| `Dockerfile.alpine` | `alpine:3` | Wie oben, aber `apk` statt `apt`; `inotify` kommt hier per `pip` (das `apk`-Paket `py3-inotify` packt ein anderes, unpassendes Projekt) |
| `Dockerfile.pyimg` | `python:3-slim` | Einstufig, alles über `pip` (inkl. `requests`/`inotify` direkt aus den in `pyproject.toml` deklarierten Dependencies) |

Alle drei Varianten lassen kein Build-Tooling (`pip`/`setuptools`, sofern nicht ohnehin Teil des Basis-Images) im finalen Laufzeit-Image zurück.

---

## 💻 CLI & Parameter Übersicht
```text
yt-upload [-h] [-v] [-a] [-D] [--healthcheck] [-t TITLE] [-d DESCRIPTION]
          [-c CATEGORY] [--tags TAGS] [--privacy {public,private,unlisted}]
          [--thumbnail PATH] [--playlist PLAYLIST] [--publish-at ISO_DATE]
          [--license {youtube,creativeCommon}] [--location LOCATION]
          [--recording-date DATE] [--default-language LANG]
          [--default-audio-language LANG] [--embeddable]
          [--credentials-file PATH] [--client-secrets PATH]
          [--chunksize BYTES] [--open-link]
          [file]
```

### Die Konfig-Datei `upload.conf`
Unter `/etc/yt-upload/` befindet sich die `upload.conf`. Diese wird sowohl im Container als auch bei einer Bare-Metal-Installation gelesen. Für Snippets steht zusätzlich das `conf.d`-Verzeichnis zur Verfügung.

```ini
# ==============================================================================
# YouTube Video Uploader Configuration Example
# Pfad: /etc/yt-upload/upload.conf oder conf.d/*.conf
# ==============================================================================

[paths]
# Verzeichnis für neu eingehende Videodateien
#in_dir = /videos/in

# Temporäres Arbeitsverzeichnis während der Verarbeitung/Splittings
#work_dir = /videos/work

# Zielverzeichnis für erfolgreich hochgeladene und archivierte Dateien
#done_dir = /videos/done

# Zielverzeichnis für fehlerhafte oder unvollständige Dateien
#corrupt_dir = /videos/corrupt

# Zielverzeichnis für Dateien, bei denen bereits mind. ein Segment erfolgreich
# hochgeladen wurde, bevor ein Fehler auftrat (getrennt von corrupt_dir, um
# Doppel-Uploads bereits hochgeladener Segmente bei einem erneuten Lauf zu vermeiden)
#retry_dir = /videos/retry

# Pfad zur zentralen Logdatei
#log_file = /log/upload.log

# Pfad zu den Google OAuth Credentials
#credentials_file = /app/oauth/youtube-upload-credentials.json


[settings]
# Erstellt/Ermittelt automatisch Playlists basierend auf dem 'ARTIST'-Tag (true/false)
#enable_dynamic_playlists = false

# Standard-Sichtbarkeit für hochgeladene Videos (public, private, unlisted)
#privacy_status = unlisted

# Standard-Kategorie (Name oder YouTube Category ID, z.B. 22)
#default_category = Entertainment

# Standardsprache für Titel, Beschreibung und Audio (ISO 639-1 Code)
#default_language = de

# Einbetten der Videos auf externen Websites erlauben (true/false)
#allow_embedding = true

# Standard-Beschreibungstext, falls keine Metadaten/CLI-Argumente vorhanden sind
#default_description = Automatischer Upload via Script.

# Standard-Tags (kommagetrennt)
#default_tags = Upload, Video

# Automatische Thumbnail-Generierung via FFmpeg aktivieren (true/false)
#auto_generate_thumbnail = true

# Mindestintervall in Sekunden für den Auto-Thumbnail Frame-Grab
#auto_thumb_min_sec = 15

# Maximalintervall in Sekunden für den Auto-Thumbnail Frame-Grab
#auto_thumb_max_sec = 120

# Bei Namenskonflikten in DONE/CORRUPT Dateien überschreiben statt umzubenennen (true/false)
#allow_overwrite = true

# Aktiviert die Zensurfunktion für die description
#enable_description_censor = false

[blacklist]
# Adult / NSFW
#onlyfans.com = true
#fansly.com = true
#loyalfans.com = true
#manyvids.com = true
#pornhub.com = true
#chaturbate.com = true

# Gambling & Skins
#stake.com = true
#csgoroll.com = true
#hellcase.com = true
#1xbet.com = true

# Adfly & Spammige Shortener
#adf.ly = true
#shorte.st = true
```

### Wichtige CLI-Flags
* `-D`, `--daemon`
  Startet den Dauerüberwachungs-Dämon via `inotify` (ohne Argumente).

* `-a`, `--auto`
  Verarbeitet alle Videos in `IN_DIR` (Standard: `/videos/in`) im Batch-Modus und beendet sich danach.

* `--healthcheck`
  Prüft nur den Heartbeat des laufenden Dämons und beendet sich sofort - für Docker `HEALTHCHECK` gedacht, nicht für den interaktiven Gebrauch.

* `-t`, `--title` `TEXT`
  Setzt explizit den Videotitel (überschreibt ausgelesene Metadaten).

* `-c`, `--category` `NAME/ID`
  Name oder YouTube Category-ID (z. B. `Entertainment`, `Gaming`, `22`).

* `-d`, `--description` `TEXT`
  Beschreibungstext für das Video.

* `--privacy` `STATUS`
  Sichtbarkeit des Videos (`public`, `unlisted` [Standard], `private`).

* `--playlist` `NAME`
  Name der Ziel-Playlist (wird automatisch erstellt, falls nicht vorhanden).

* `--chunksize` `BYTES`
  Chunk-Größe für Resumable Uploads in Bytes (Standard: `268435456` = 256 MiB, auf ein Vielfaches von 256 KiB gerundet - zwingende Vorgabe der YouTube API).

* `--open-link`
  Öffnet nach erfolgreichem Upload die Video-URL im Standardbrowser.

---

## ⚙️ Umgebungsvariablen (Environment)

* `ENABLE_DYNAMIC_PLAYLISTS`
  * **Standard:** `false`
  * **Beschreibung:** Bei `true` wird der `ARTIST`-Tag aus den Videometadaten automatisch als Playlist-Name genutzt.

* `TZ`
  * **Standard:** `UTC`
  * **Beschreibung:** Zeitzone für Logs und Anwendungszeitstempel (z. B. `Europe/Berlin`).

* `HOME`
  * **Standard:** `/app`
  * **Beschreibung:** Pfad für benutzerspezifische Konfigurationen/Caches (im Container meist `/tmp` für Read-Only-Support).

* `DEBUG`
  * **Standard:** `0`
  * **Beschreibung:** Setze auf `true` oder `1`, um erweiterte Log-Ausgaben für Entwickler zu aktivieren.

Sämtliche Pfade aus dem `[paths]`-Abschnitt der `upload.conf` (siehe oben) lassen sich zusätzlich per gleichnamiger, großgeschriebener Umgebungsvariable überschreiben (z. B. `RETRY_DIR`, `LOG_FILE`, `CREDENTIALS_FILE`) - Umgebungsvariablen haben dabei immer Vorrang vor der Konfigurationsdatei.

---

## 📂 Verzeichnisstruktur im Container

* `/videos/in`: Eingangsverzeichnis für neue Videodateien.
* `/videos/work`: Temporäres Arbeitsverzeichnis während Analyse, Splitting und Upload.
* `/videos/done`: Archivverzeichnis für erfolgreich verarbeitete Originaldateien.
* `/videos/corrupt`: Zielverzeichnis für beschädigte, nicht lesbare oder komplett fehlgeschlagene Videodateien.
* `/videos/retry`: Zielverzeichnis für Dateien mit Teilfortschritt (mind. ein Segment bereits hochgeladen, dann ein Fehler) - manuell zurück nach `/videos/in` verschieben, um den Rest nachzuholen.

---

## 🧑‍💻 Lokaler Build & Entwicklung

Ein neues Docker-Image kann über das mitgelieferte Shell-Skript gebaut werden:

```bash
./build.sh                    # Nutzt Name & Version aus pyproject.toml, Standard-Dockerfile
./build.sh 2.0.0               # Explizite Version, Standard-Dockerfile
./build.sh alpine               # Version aus pyproject.toml, Dockerfile.alpine
./build.sh 2.0.0 pyimg          # Explizite Version, Dockerfile.pyimg
```

Name und Standard-Version werden automatisch aus `pyproject.toml` gelesen - ein explizit übergebenes Versions-Argument überschreibt das.

### Tests & Linting
```bash
apt install python3-pytest   # oder: pip install -r requirements-test.txt
pytest -v

ruff check src/yt_upload/
```

---

## 📄 Lizenz

Dieses Projekt steht unter der GNU General Public License v3.0 (GPLv3).

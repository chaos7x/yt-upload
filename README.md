# yt-upload 📹☁️

`yt-upload` (v2.0.0) ist eine Python- und Docker-basierte Automatisierungslösung zur ereignisbasierten Überwachung von Video-Verzeichnissen und zum automatischen Upload auf YouTube via native REST API v3, FFmpeg und inotify.

Das Tool verarbeitet eingehende Videodateien, extrahiert eingebettete Metadaten sowie Cover-Thumbnails, zerlegt Videos bei Bedarf verlustfrei in Segmente (z. B. bei Dateien > 10 Stunden) und ordnet sie dynamisch Playlists zu.

---

## ✨ Features

* **Direkte HTTP REST API v3:** Native Implementierung für Resumable Chunk-Uploads ohne schwerfällige externe API-Wrapper.
* **Inotify-Ordnerüberwachung:** Überwacht `/videos/in` im Dämon-Modus (`-D`) in Echtzeit auf Dateiveränderungen (`.mp4`, `.mkv`, `.mov`, `.m4v`) inklusive Polling-Fallback.
* **Drei flexible Betriebsmodi:**
  1. **Dämon-Modus (`-D` / `--daemon`):** Dauerhafter Hintergrunddienst zur automatischen Überwachung.
  2. **Auto-Batch (`-a` / `--auto`):** Einmaliges Abarbeiten eines Verzeichnisses mit anschließendem Beenden.
  3. **Manuell (CLI):** Upload einzelner Videodateien mit voller Parameterkontrolle.
* **Metadaten- & Thumbnail-Extraktion:** Liest Titel, Beschreibung, PURL, Genre, Aufnahmedatum und Artist via `ffprobe` aus. Extrahiert automatisch Thumbnails aus MKV-Attachments, MP4-Covern oder generiert ein Frame-Thumbnail.
* **Verlustfreies FFmpeg-Splitting:** Zertrennt Videos mit einer Laufzeit von über 10 Stunden (36.000 Sekunden) automatisch und ohne Qualitätsverlust (`-c copy`) in durchnummerierte Segmente.
* **Dynamische Playlist-Verwaltung:** Erstellt und verknüpft Ziel-Playlists automatisch (z. B. auf Basis des `ARTIST`-Tags via `ENABLE_DYNAMIC_PLAYLISTS`).
* **Robustes Retry & Health-Check:** Prüft Videodateien vor dem Upload auf unvollständige Schreibvorgänge / fehlende `moov`-Atome und wiederholt abgebrochene Upload-Sessions automatisch.

---

## 🚀 Schnellstart

### 1. Ordnerstruktur anlegen
Erstelle die benötigten Ordnerstrukturen auf deinem Host-System:

```bash
mkdir -p videos/in videos/work videos/done videos/corrupt log oauth etc/yt-upload/conf.d
Erstelle die benötigten Ordnerstrukturen auf deinem Host-System:
```
Hinterlege deine Google OAuth Secrets unter oauth/client_secrets.json. Eine Beispielvorlage für die Datei:
```json
{
  "installed": {
    "client_id": "DEINE_CLIENT_ID.apps.googleusercontent.com",
    "project_id": "DEIN_PROJECT_ID",
    "auth_uri": "[https://accounts.google.com/o/oauth2/auth](https://accounts.google.com/o/oauth2/auth)",
    "token_uri": "[https://oauth2.googleapis.com/token](https://oauth2.googleapis.com/token)",
    "auth_provider_x509_cert_url": "[https://www.googleapis.com/oauth2/v1/certs](https://www.googleapis.com/oauth2/v1/certs)",
    "client_secret": "DEIN_CLIENT_SECRET",
    "redirect_uris": ["http://localhost"]
  }
}
```
2. OAuth-Token generieren

Führe das mitgelieferte Authentifizierungsskript im Container aus:
```bash
docker run --rm -it \
  -v $(pwd)/oauth:/app/oauth \
  ghcr.io/chaos7x/yt-upload:latest /usr/local/bin/get_token
```
3. Starten via Docker CLI
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
  -v $(pwd)/config:/etc/yt-upload:rw
  ghcr.io/chaos7x/yt-upload:latest
```
📦 Docker Compose Integration
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
      - DESCRIPTION_BLACKLIST="onlyfans.com,fansly.com,loyalfans.com,manyvids.com,pornhub.com,chaturbate.com,stake.com,csgoroll.com,hellcase.com,1xbet.com,adf.ly,shorte.st"
    env_file:
      - .env
    volumes:
      - oauth:/app/oauth
      - videos:/videos
      - log:/log
      - config:/etc/yt-upload

    tty: true
    stdin_open: true

volumes:
  app:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: "./app"

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
💻 CLI & Parameter Übersicht
```text
yt-upload [-h] [-v] [-a [PATH]] [-D] [-t TITLE] [-c CATEGORY] [-d DESCRIPTION]
          [--description-file PATH] [--tags TAGS] [--privacy {public,unlisted,private}]
          [--publish-at ISO_DATE] [--license {youtube,creativeCommon}]
          [--location LOCATION] [--recording-date ISO_DATE]
          [--default-language LANG] [--default-audio-language LANG]
          [--thumbnail PATH] [--playlist PLAYLIST] [--title-template TEMPLATE]
          [--embeddable | --no-embeddable] [--client-secrets PATH]
          [--credentials-file PATH] [--chunksize BYTES] [--open-link]
          [files ...]
```
Die Konfig-Datei upload.conf
Unter /etc/yt-upload/ befidet sich die upload.conf. Dies ist dafür gedacht, wenn der Uploader nicht im Docker-Container, sondern nativ auf dem OS läuft. 
Für snipsets steht das conf.d Verzeichnis zur Verfügung
Benötigt werden die Pakete "python3-requests, python3-inotify"
```bash
apt install python3-requests python3-inotify
```

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
```markdown
* `-D`, `--daemon`
  Startet den Dauerüberwachungs-Dämon via `inotify` (ohne Argumente).

* `-a`, `--auto` `[PATH]`
  Verarbeitet alle Videos in `PATH` (Standard: `/videos/in`) im Batch-Modus und beendet sich danach.

* `-t`, `--title` `TEXT`
  Setzt explizit den Videotitel (überschreibt ausgelesene Metadaten).

* `-c`, `--category` `NAME/ID`
  Name oder YouTube Category-ID (z. B. `Entertainment`, `Gaming`, `22`).

* `-d`, `--description` `TEXT`
  Beschreibungstext für das Video.

* `--description-file` `PATH`
  Liest die Videobeschreibung aus einer angegebenen Textdatei.

* `--privacy` `STATUS`
  Sichtbarkeit des Videos (`public`, `unlisted` [Standard], `private`).

* `--playlist` `NAME`
  Name der Ziel-Playlist (wird automatisch erstellt, falls nicht vorhanden).

* `--chunksize` `BYTES`
  Chunk-Größe für Resumable Uploads in Bytes (Standard: `104857600` = 100MB).

* `--title-template` `MUSTER`
  Template für gesplittete oder multiple Videos (Standard: `{title} [{n}/{total}]`).
```

## ⚙️ Umgebungsvariablen (Environment)
```markdown
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
```
📂 Verzeichnisstruktur im Container

    /videos/in: Eingangsverzeichnis für neue Videodateien[cite: 5].

    /videos/work: Temporäres Arbeitsverzeichnis während der Analyse, Splittings und des Uploads[cite: 5].

    /videos/done: Archivverzeichnis für erfolgreich verarbeitete Originaldateien[cite: 5].

    /videos/corrupt: Zielverzeichnis für beschädigte oder nicht lesbare Videodateien[cite: 5].

🛠️ Lokaler Build & Entwicklung

Ein neues Docker-Image kann über das mitgelieferte Shell-Skript gebaut werden:
```bash
./build.sh v2.0.0
```
Das Skript baut das Image unter Verwendung des Multi-Stage Dockerfiles (inklusive statischer ffmpeg/ffprobe-Binaries).

📄 Lizenz

Dieses Projekt steht unter der GNU General Public License v3.0 (GPLv3).


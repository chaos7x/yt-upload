# yt-upload 📹☁️

`yt-upload` ist eine Python- und Docker-basierte Automatisierungslösung zur ereignisbasierten Überwachung von Video-Verzeichnissen und zum automatischen Upload auf YouTube via native REST API v3, FFmpeg und inotify.

Das Tool verarbeitet eingehende Videodateien, extrahiert eingebettete Metadaten sowie Cover-Thumbnails, zerlegt Videos bei Bedarf verlustfrei in Segmente (z. B. bei Dateien > 10 Stunden) und ordnet sie dynamisch Playlists zu.

---

## ✨ Features

* **Direkte HTTP REST API v3:** Native Implementierung für Resumable Chunk-Uploads ohne schwerfällige externe API-Wrapper.
* **Inotify-Ordnerüberwachung:** Überwacht `IN_DIR` im Dämon-Modus (`-D`) in Echtzeit auf Dateiveränderungen (`.mp4`, `.mkv`, `.mov`, `.m4v`) inklusive Polling-Fallback, falls `inotify` nicht verfügbar ist.
* **Drei flexible Betriebsmodi:**
  1. **Dämon-Modus (`-D` / `--daemon`):** Dauerhafter Hintergrunddienst zur automatischen Überwachung.
  2. **Auto-Batch (`-a` / `--auto`):** Einmaliges Abarbeiten eines Verzeichnisses mit anschließendem Beenden.
  3. **Manuell (CLI):** Upload einzelner Videodateien mit voller Parameterkontrolle.
* **Metadaten- & Thumbnail-Extraktion:** Liest Titel, Beschreibung, PURL, Genre, Aufnahmedatum und Artist via `ffprobe` aus. Extrahiert automatisch Thumbnails aus MKV-Attachments, MP4-Covern oder generiert ein Frame-Thumbnail.
* **Verlustfreies FFmpeg-Splitting:** Zertrennt Videos mit einer Laufzeit von über 10 Stunden (36.000 Sekunden) automatisch und ohne Qualitätsverlust (`-c copy`) in durchnummerierte Segmente.
* **Dynamische Playlist-Verwaltung:** Erstellt und verknüpft Ziel-Playlists automatisch (z. B. auf Basis des `ARTIST`-Tags via `ENABLE_DYNAMIC_PLAYLISTS`).
* **Robustes Retry & Health-Check:** Prüft Videodateien vor dem Upload auf unvollständige Schreibvorgänge / fehlende `moov`-Atome. Bereits erfolgreich hochgeladene Segmente werden bei einem Fehler im nächsten Segment nicht erneut hochgeladen (Fortschritt wird pro Datei persistiert) - fehlgeschlagene Jobs mit Teilfortschritt oder einem reinen API-Kontingent-Limit (`quotaExceeded`) landen in `RETRY_DIR`, komplett fehlerhafte in `CORRUPT_DIR`. Ein optionaler `yt-upload-retry.timer` (siehe unten) verschiebt `RETRY_DIR`-Inhalte periodisch automatisch zurück nach `IN_DIR`.

Eine Übersicht der internen Architektur (Module, Datenfluss, Diagramm) findet sich in [ARCHITECTURE.md](ARCHITECTURE.md).

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

Standardmäßig zeigt `get-token`/`get_token` einen Link an, den du im Browser öffnest; nach dem Login bricht die Seite ab (Seite nicht gefunden) - kopiere in dem Fall die komplette URL aus der Adresszeile zurück ins Terminal. Dieser Copy-Paste-Schritt ist bewusst der Standard, da er unabhängig davon funktioniert, ob `get-token` und dein Browser auf demselben Rechner laufen (z. B. Docker auf einem entfernten Server/NAS, Browser auf deinem PC).

Läuft `get-token` dagegen auf demselben Rechner wie dein Browser (z. B. Bare-Metal-Desktop-Nutzung, oder Docker mit explizit publiziertem Port `8080`), kannst du den Redirect automatisch abfangen lassen und dir das Copy-Paste sparen:

```bash
OAUTH_LOCAL_SERVER=true get-token
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
  -v $(pwd)/yt-upload-data:/srv/media-pipeline:rw \
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
      # Kompletter Datenzustand (incoming/work/done/corrupt/retry) in einem
      # einzigen Bind-Mount direkt auf /srv/media-pipeline - trifft damit
      # exakt den Code-Default, keine ENV-Umbiegung nötig. "incoming" ist
      # der einzige Unterordner davon, in den auch fetchbridge hineinschreibt.
      - yt-upload-data:/srv/media-pipeline
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

  yt-upload-data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: "./yt-upload-data"

  log:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: "./log"
```

#### 🔗 Interop mit Bare-Metal (gemeinsamer Host-Pfad)

`user: "11107:11108"` oben ist nur ein Platzhalter. Läuft `fetchbridge`/`tw-recorder` (oder beide) als Bare-Metal-/`.deb`-Installation statt als Container, mountest du `yt-upload-data` statt auf `./yt-upload-data` direkt auf den echten Host-Pfad `/srv/media-pipeline` (`device: "/srv/media-pipeline"` oben, oder bei `docker run` direkt `-v /srv/media-pipeline:/srv/media-pipeline:rw`).

`/srv/media-pipeline` gehört dort `root:media-pipeline` mit Modus `2775` (setgid, bewusst **ohne** Sticky-Bit) - Schreib-/Löschrecht hängt also rein an der **Gruppe**, nicht an der UID oder dem Datei-Owner. Die GID im `user:`-Feld muss deshalb mit der echten Host-Gruppe übereinstimmen, sonst gibt's `Permission denied`:

```bash
getent group media-pipeline   # z.B. media-pipeline:x:998:
```

Die zweite Zahl in `user: "<uid>:<gid>"` durch diese echte GID ersetzen (z.B. `user: "11107:998"`) - die UID (erste Zahl) ist frei wählbar, da sie für die Zugriffsrechte auf dieses Verzeichnis keine Rolle spielt.

### Optional: Automatischer Retry liegen gebliebener Dateien (Docker)

Die systemd-Timer-/Cron-Lösung aus dem Bare-Metal-Abschnitt (siehe unten) greift in Docker nicht - der Container läuft als Single-Process ohne eigenen Cron/systemd. `--requeue-retries` selbst funktioniert aber unverändert, da es keinen Instanz-Lock braucht und daher problemlos neben dem bereits laufenden `-D`-Prozess im selben Container ausgeführt werden kann - die Terminierung übernimmt stattdessen der **Docker-Host** per `docker exec` in den laufenden Container hinein:

```bash
# Host-Crontab (crontab -e auf dem Docker-Host)
0 3 * * * docker exec yt-upload yt-upload --requeue-retries
```

Nutzt der Host selbst systemd, geht das genauso als Timer (analog zu `yt-upload-retry.timer`, nur mit `docker exec` statt direktem Aufruf):

```ini
# /etc/systemd/system/yt-upload-retry-docker.service
[Unit]
Description=yt-upload (Docker) - Requeue liegen gebliebener Dateien aus RETRY_DIR

[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec yt-upload yt-upload --requeue-retries
```

```ini
# /etc/systemd/system/yt-upload-retry-docker.timer
[Unit]
Description=Periodischer Requeue liegen gebliebener Dateien aus RETRY_DIR (yt-upload, Docker)

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

(`yt-upload` im `docker exec`-Aufruf ist hier der `container_name` aus der Compose-Datei oben, nicht der Befehl - bei abweichendem Namen entsprechend anpassen.)

#### Alternative: Scheduler-Sidecar in der docker-compose.yaml (ofelia)

Wer die Terminierung lieber komplett in der `docker-compose.yaml` selbst abbilden will statt auf dem Host, kann einen Scheduler-Sidecar wie [ofelia](https://github.com/mcuadros/ofelia) ergänzen, der per Labels denselben `docker exec`-Aufruf übernimmt:

```yaml
services:
  yt-upload:
    # ... wie oben ...
    labels:
      ofelia.enabled: "true"
      ofelia.job-exec.retry.schedule: "@daily"
      ofelia.job-exec.retry.command: "yt-upload --requeue-retries"

  ofelia:
    image: mcuadros/ofelia:latest
    command: daemon --docker
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    depends_on:
      - yt-upload
```

> **⚠️ Warnung:** Das setzt Zugriff auf den Docker-Socket (`/var/run/docker.sock`) im `ofelia`-Container voraus - wer den Socket kontrolliert, kann darüber **jeden** Container auf dem Host starten, stoppen und inspizieren, faktisch also Root-Rechte auf dem gesamten Host, nicht nur auf `yt-upload`. Selbst `:ro` (read-only) mountet nur die Socket-*Datei* schreibgeschützt, verhindert aber nicht, dass die Docker-API darüber beliebige neue, privilegierte Container starten kann. Für die meisten Setups ist der schlankere Host-Cron/-Timer von oben (kein zusätzlicher Container, kein Socket-Mount) deshalb die sicherere Wahl - dieser Weg ist nur für Umgebungen gedacht, die dieses Risiko bewusst eingehen wollen.

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

Danach stehen die Befehle `yt-upload` und `get-token` systemweit zur Verfügung (`yt-upload --version` zum Testen). `get-token` führt denselben interaktiven OAuth-Flow wie im Docker-Setup (Schritt 3 oben) aus, liest `client_secrets.json` dabei aber standardmäßig von `/etc/yt-upload/client_secrets.json` (per `CLIENT_SECRETS_FILE`-Umgebungsvariable anpassbar) und schreibt die Credentials nach `/etc/yt-upload/youtube-upload-credentials.json` - derselbe Pfad, den auch der `yt-upload`-Dienst selbst als Standard erwartet (keine `/app/oauth`-Docker-Konvention auf Bare-Metal). Existiert bereits der dedizierte `yt-upload`-Systemuser (Dämon-Paket installiert), chownt `get-token` die Datei automatisch auf ihn, damit der Dämon sie lesen kann, auch wenn `get-token` selbst als root/Admin lief. Für ein Update genügt ein erneuter `pip install ...`-Aufruf im aktualisierten Repo-Verzeichnis.

Die Logdatei landet je nach Umgebung automatisch am sinnvollsten Ort (`/var/log/yt-upload/`, sofern beschreibbar und ein klassischer Syslog-Daemon läuft, sonst nur auf `stdout`/journald) - siehe `LOG_FILE`-Umgebungsvariable, falls ein fester Pfad gewünscht ist. Läuft ein Syslog-Daemon, rotiert die App die Datei bewusst **nicht** selbst (kein `RotatingFileHandler`) - das übernimmt das mitgelieferte `/etc/logrotate.d/yt-upload` (nur im `.deb`-Paket enthalten; bei einer reinen `pip`-Installation ohne `.deb` selbst einrichten, falls gewünscht). Nur bei explizit gesetztem `LOG_FILE` oder einem gemounteten Docker-`/log`-Volume rotiert die App eigenständig, da dort sonst niemand rotieren würde.

### Alternative: Fertiges Debian-Paket (.deb)

Jedes [GitHub Release](https://github.com/chaos7x/yt-upload/releases) enthält zwei `.deb`-Anhänge, aufgeteilt nach Nutzung - keine manuelle `pip`-Installation nötig:

* **`yt-upload_<version>_all.deb`** - CLI, Python-Package und Config (`/etc/yt-upload/upload.conf`). Reicht für den rein manuellen Datei-Modus (`yt-upload video.mp4 ...`) und Auto-Batch (`-a`). `apt`/`dpkg` löst `python3-requests`/`ffmpeg` automatisch mit auf.
* **`yt-upload-daemon_<version>_all.deb`** - nur für den Dämon-Modus (`-D`) nötig: systemd-Service, dedizierter Systemuser (`yt-upload`) und die `python3-inotify`-Abhängigkeit für die Echtzeit-Ordnerüberwachung. Hängt von `yt-upload` in exakt derselben Version ab, zieht es also automatisch mit.

Nur die CLI ohne Dämon-Overhead (Systemuser, `python3-inotify`, nie aktivierter systemd-Service):

```bash
wget https://github.com/chaos7x/yt-upload/releases/latest/download/yt-upload_<version>_all.deb
apt install ./yt-upload_<version>_all.deb
```

Für den Dauerbetrieb zusätzlich das Daemon-Paket installieren (zieht `yt-upload` automatisch nach, falls noch nicht vorhanden):

```bash
wget https://github.com/chaos7x/yt-upload/releases/latest/download/yt-upload-daemon_<version>_all.deb
apt install ./yt-upload-daemon_<version>_all.deb
```

Das Daemon-Paket legt den Systemuser und den systemd-Service an, startet ihn aber bewusst nicht automatisch. `IN_DIR`/`WORK_DIR`/`DONE_DIR`/`CORRUPT_DIR`/`RETRY_DIR` zeigen automatisch einheitlich (Docker wie Bare-Metal) auf `/srv/media-pipeline/{incoming,work,done,corrupt,retry}` - `IN_DIR` ist davon dasselbe Verzeichnis, in das `fetchbridge`s `TARGET_DIR` schreibt, die anderen vier sind rein interner Zustand. Das `.deb`-Postinst legt alle fünf mit der gemeinsamen Gruppe (`media-pipeline`) an. Wer davon abweichende Pfade will, kann sie wie gehabt über `[paths]` in `/etc/yt-upload/upload.conf` überschreiben. Vor dem ersten Start noch `get-token` einmalig manuell ausführen (kein Service, siehe oben), dann:

```bash
systemctl enable --now yt-upload
```

**Devuan / Debian ohne systemd (`sysvinit-core`):** Das Daemon-Paket bringt zusätzlich ein klassisches `/etc/init.d/yt-upload`-Skript mit, das `daemon-postinst` automatisch anstelle des systemd-Service registriert, wenn kein systemd läuft:

```bash
service yt-upload start
```

#### Optional: Automatischer Retry liegen gebliebener Dateien

Dateien in `RETRY_DIR` (z.B. nach einem tagesaktuellen API-Kontingent-Limit `quotaExceeded`, oder mit bereits teilweise hochgeladenen Segmenten) bleiben dort, bis sie manuell zurück nach `IN_DIR` verschoben werden. Das `yt-upload-daemon`-Paket bringt dafür optional einen systemd-Timer mit, der das automatisch übernimmt:

```bash
systemctl enable --now yt-upload-retry.timer
```

Läuft standardmäßig einmal täglich (`OnCalendar=daily`, siehe `yt-upload-retry.timer` - passend zu Googles täglichem Kontingent-Reset, per `systemctl edit yt-upload-retry.timer` beliebig anpassbar) und ruft dabei nur `yt-upload --requeue-retries` auf - ein einmaliger, kurzlebiger Aufruf ohne eigenen Cooldown im Code: ein zu früh erneut versuchter `quotaExceeded`-Fall scheitert einfach sofort wieder (kostet kein zusätzliches Kontingent) und landet erneut in `RETRY_DIR`. Das Timer-Intervall ist damit die einzige Stellschraube für die Retry-Kadenz. Bereits hochgeladene Segmente gehen dabei nie verloren (Fortschritt wird anhand des Dateinamens automatisch wiedergefunden).

Ohne systemd übernimmt cron dieselbe Aufgabe: `/etc/cron.d/yt-upload-retry` wird bereits mitinstalliert, allerdings standardmäßig deaktiviert (die Zeitplan-Zeile ist auskommentiert) - zum Aktivieren einfach einkommentieren:

```bash
sed -i 's/^#0 3/0 3/' /etc/cron.d/yt-upload-retry
```

### Alternative: Standalone .pyz (kein pip/apt nötig)

`./build-pyz.sh` baut aus `src/` je ein selbst-enthaltenes `.pyz` pro Eintrag in `[project.scripts]` (`yt-upload.pyz` und `get-token.pyz`) samt `requests` und optional `inotify` - läuft auf jedem System mit einem nackten `python3`, ganz ohne vorherige `pip install`/`apt install`:

```bash
./build-pyz.sh
./yt-upload.pyz --version
./get-token.pyz
```

### 🔒 Optional: Credentials mit systemd-creds verschlüsseln (nur Dämon/.deb-Paket)

`CREDENTIALS_FILE` ist per Env-Var überschreibbar (`config.py`) - das lässt sich mit `LoadCredentialEncrypted=` (systemd >= 250) kombinieren, um die OAuth-Credentials-Datei nicht mehr dauerhaft als Klartext auf der Platte liegen zu haben. Anders als eine App-seitige Verschlüsselung mit Schlüssel direkt daneben ist das ein echter Gewinn: der `yt-upload`-Systemuser selbst braucht dafür nie Lesezugriff auf den Master-Key - nur `systemd` (PID 1, root) entschlüsselt beim Service-Start und reicht dem Prozess ausschließlich eine Kopie in einem privaten, nur für ihn lesbaren tmpfs-Verzeichnis durch.

`get-token` und der `yt-upload`-Dienst nutzen bare-metal standardmäßig beide `/etc/yt-upload/youtube-upload-credentials.json` (ohne `/app/oauth`, das ist nur die Docker-Konvention) - keine manuelle `CREDENTIALS_FILE`-Konfiguration nötig, bevor es weitergeht:

Am saubersten über ein Override-Snippet statt direkt in der von `.deb`/systemd verwalteten Unit-Datei (bleibt so update-sicher):

```bash
# 1. Vorhandene Klartext-Datei verschlüsseln (einmalig, als root; --with-key=tpm2 bindet
#    die .cred-Datei zusätzlich an dieses eine Gerät, sonst wird automatisch ein
#    maschinen-eigener Schlüssel unter /var/lib/systemd/credential.secret verwendet)
systemd-creds encrypt \
  --name=youtube-credentials \
  /etc/yt-upload/youtube-upload-credentials.json \
  /etc/yt-upload/youtube-upload-credentials.json.cred

# 2. Override-Datei anlegen statt die Unit direkt zu editieren
systemctl edit yt-upload
```

Im Editor, der sich dabei öffnet, folgendes Override-Snippet einfügen:

```ini
[Service]
LoadCredentialEncrypted=youtube-credentials:/etc/yt-upload/youtube-upload-credentials.json.cred
Environment=CREDENTIALS_FILE=%d/youtube-credentials
```

(`%d` ist der systemd-Specifier für `$CREDENTIALS_DIRECTORY`, das private tmpfs mit der entschlüsselten Kopie.)

```bash
# 3. Erst NACH erfolgreichem Test (systemctl restart yt-upload, Logs prüfen) das
#    Original entfernen - shred statt rm, damit nichts unverschlüsselt auf der SSD/im
#    Dateisystem-Journal hängen bleibt
systemctl restart yt-upload
shred -u /etc/yt-upload/youtube-upload-credentials.json
```

`get-token` selbst bleibt davon unberührt - es muss weiterhin einmalig interaktiv laufen und die Klartext-Datei erst erzeugen, bevor sie in Schritt 1 verschlüsselt wird.

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
yt-upload [-h] [-v] [-a] [-D] [--healthcheck] [--requeue-retries] [-t TITLE]
          [--title-template TEMPLATE]
          [-d DESCRIPTION | --description-file PATH]
          [-c CATEGORY] [--tags TAGS] [--privacy {public,private,unlisted}]
          [--thumbnail PATH] [--playlist PLAYLIST] [--publish-at ISO_DATE]
          [--license {youtube,creativeCommon}] [--location LOCATION]
          [--recording-date DATE] [--default-language LANG]
          [--default-audio-language LANG] [--embeddable {true,false}]
          [--credentials-file PATH] [--client-secrets PATH]
          [--chunksize BYTES] [--open-link]
          [file ...]
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
#in_dir = /srv/media-pipeline/incoming

# Temporäres Arbeitsverzeichnis während der Verarbeitung/Splittings
#work_dir = /srv/media-pipeline/work

# Zielverzeichnis für erfolgreich hochgeladene und archivierte Dateien
#done_dir = /srv/media-pipeline/done

# Zielverzeichnis für fehlerhafte oder unvollständige Dateien
#corrupt_dir = /srv/media-pipeline/corrupt

# Zielverzeichnis für Dateien, bei denen bereits mind. ein Segment erfolgreich
# hochgeladen wurde, bevor ein Fehler auftrat (getrennt von corrupt_dir, um
# Doppel-Uploads bereits hochgeladener Segmente bei einem erneuten Lauf zu vermeiden)
#retry_dir = /srv/media-pipeline/retry

# Pfad zur zentralen Logdatei
#log_file = /log/upload.log

# Pfad zu den Google OAuth Credentials. Ohne diese Angabe: /app/oauth/... in
# Docker (siehe dortiges Bind-Mount), sonst /etc/yt-upload/... auf Bare-Metal.
#credentials_file = /etc/yt-upload/youtube-upload-credentials.json


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
  Verarbeitet alle Videos in `IN_DIR` (Standard: `/srv/media-pipeline/incoming`) im Batch-Modus und beendet sich danach.

* `--healthcheck`
  Prüft nur den Heartbeat des laufenden Dämons und beendet sich sofort - für Docker `HEALTHCHECK` gedacht, nicht für den interaktiven Gebrauch.

* `--requeue-retries`
  Verschiebt alle Dateien aus `RETRY_DIR` zurück nach `IN_DIR` und beendet sich sofort - für einen periodischen systemd-Timer gedacht (siehe `yt-upload-retry.timer` weiter oben), kann aber auch manuell aufgerufen werden.

* `-t`, `--title` `TEXT`
  Setzt explizit den Videotitel (überschreibt ausgelesene Metadaten).

* `file [file ...]`
  Mehrere Videodateien in einem Aufruf hochladen (`yt-upload video1.mp4 video2.mp4 ...`), nacheinander mit denselben Metadaten. Bricht auf einmal ab, falls eine der Dateien nicht existiert, statt teilweise zu verarbeiten.

* `--title-template` `TEMPLATE` (Standard: `{title} (Teil {n}/{total})`)
  Nur wirksam bei mehreren Dateien **und** explizit gesetztem `-t`/`--title`: numeriert den gemeinsamen Titel pro Datei durch (Platzhalter `{title}`, `{n}`, `{total}`). Ohne explizites `-t` behält jede Datei ihren eigenen, aus Metadaten/Dateiname abgeleiteten Titel.

* `-c`, `--category` `NAME/ID`
  Name oder YouTube Category-ID (z. B. `Entertainment`, `Gaming`, `22`).

* `-d`, `--description` `TEXT`
  Beschreibungstext für das Video.

* `--description-file` `PATH`
  Liest den Beschreibungstext stattdessen aus einer Textdatei (z. B. für längere, mehrzeilige Beschreibungen ohne Shell-Escaping). Schließt sich mit `-d`/`--description` gegenseitig aus.

* `--embeddable` `{true,false}`
  Erlaubt (`true`, Standard) oder verbietet (`false`) das Einbetten des Videos auf externen Webseiten.

* `--privacy` `STATUS`
  Sichtbarkeit des Videos (`public`, `unlisted` [Standard], `private`).

* `--playlist` `NAME`
  Name der Ziel-Playlist (wird automatisch erstellt, falls nicht vorhanden).

* `--chunksize` `BYTES`
  Chunk-Größe für Resumable Uploads in Bytes (Standard: `268435456` = 256 MiB, auf ein Vielfaches von 256 KiB gerundet - zwingende Vorgabe der YouTube API). Läuft `yt-upload` interaktiv in einem echten Terminal, zeigt sich pro Chunk-Grenze ein Live-Fortschrittsbalken (Prozent, Transferrate, ETA) auf stderr statt der sonst üblichen Logzeile - für einen glatteren Balken kleinere Werte wählen. Im Dämon-/Container-Betrieb (kein TTY) bleibt es bei der Logzeile.

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

* `LOG_LEVEL`
  * **Standard:** `INFO`
  * **Beschreibung:** `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (case-insensitive), ein ungültiger Wert fällt sicher auf den Standard zurück. Hat immer Vorrang vor `DEBUG`.
* `DEBUG`
  * **Standard:** `0`
  * **Beschreibung:** Setze auf `true` oder `1`, um erweiterte Log-Ausgaben für Entwickler zu aktivieren - abwärtskompatible Kurzform für `LOG_LEVEL=DEBUG`, nur wirksam falls `LOG_LEVEL` nicht gesetzt ist.

Sämtliche Pfade aus dem `[paths]`-Abschnitt der `upload.conf` (siehe oben) lassen sich zusätzlich per gleichnamiger, großgeschriebener Umgebungsvariable überschreiben (z. B. `RETRY_DIR`, `LOG_FILE`, `CREDENTIALS_FILE`) - Umgebungsvariablen haben dabei immer Vorrang vor der Konfigurationsdatei.

---

## 📂 Verzeichnisstruktur im Container

* `/srv/media-pipeline/incoming`: Eingangsverzeichnis für neue Videodateien - dasselbe Verzeichnis, in das `fetchbridge`s `TARGET_DIR` schreibt.
* `/srv/media-pipeline/work`: Temporäres Arbeitsverzeichnis während Analyse, Splitting und Upload.
* `/srv/media-pipeline/done`: Archivverzeichnis für erfolgreich verarbeitete Originaldateien.
* `/srv/media-pipeline/corrupt`: Zielverzeichnis für beschädigte, nicht lesbare oder komplett fehlgeschlagene Videodateien.
* `/srv/media-pipeline/retry`: Zielverzeichnis für Dateien mit Teilfortschritt (mind. ein Segment bereits hochgeladen, dann ein Fehler) - manuell zurück nach `/srv/media-pipeline/incoming` verschieben, um den Rest nachzuholen.

`work`/`done`/`corrupt`/`retry` sind rein interner Zustand von yt-upload - kein anderer Dienst liest oder schreibt dort, sie liegen nur der Einfachheit halber im selben `/srv/media-pipeline`-Namespace wie `incoming`.

---

## 🏷️ Versionierung

Reguläre Releases folgen `vX.Y.Z` (SemVer) und entstehen manuell zusammen mit einer echten Code-Änderung.

Zusätzlich prüft ein monatlicher Workflow (`os-patch-release.yml`, 1. jeden Monats), ob das Debian-/Alpine-Basis-Image ungenutzte Security-Patches hat, die `docker-refresh.yml`'s wöchentliches `latest`-Update zwar schon mitnimmt, die aber an den fixen `vX.Y.Z`-Tags vorbeilaufen (die frieren für immer auf ihrem Build-Zeitpunkt ein). Findet der Workflow etwas, hängt er eine **vierte Versionsstelle** an, die ausschließlich für solche reinen OS-Patch-Releases reserviert ist: `v1.5.1` → `v1.5.1.1` → `v1.5.1.2` (jeweils ohne Code-Änderung, nur aktualisierte System-Pakete). Bleibt die vierte Stelle bei Nichts-zu-patchen-Läufen einfach aus, gibt es auch keinen neuen Tag - kein Rauschen in der Release-Historie.

Der nächste echte Code-Release setzt diese vierte Stelle **nicht fort**, sondern lässt sie weg: auf `v1.5.1.2` folgt bei einer echten Änderung `v1.5.2`, nicht `v1.5.2.0` oder `v1.5.1.3`.

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

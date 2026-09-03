# yt-upload 📹☁️

`yt-upload` ist eine Python- und Docker-basierte Automatisierungslösung zur ereignisbasierten Überwachung von Video-Verzeichnissen und zum automatischen Upload auf YouTube via Google OAuth API, FFmpeg und FFprobe[cite: 16, 23].

Das Tool verarbeitet eingehende Videodateien, extrahiert eingebettete Metadaten sowie Cover-Thumbnails, zerlegt Videos bei Bedarf verlustfrei in Segmente (z. B. bei Dateien > 10 Stunden) und ordnet sie dynamisch Playlists zu[cite: 16].

---

## 🚀 Schnellstart

### 1. Vorbereitung (Ordner & OAuth)
Erstelle die benötigten Ordnerstrukturen auf deinem Host-System:

mkdir -p videos/in videos/work videos/done videos/corrupt log oauth

Hinterlege deine Google-Client-Geheimnisse unter `oauth/client_secrets.json`[cite: 16].

### 2. OAuth-Token generieren
Führe das Hilfsskript im Container aus, um den Erstimperativ zur Authentifizierung durchzuführen[cite: 17, 23]:

docker run --rm -it \
  -v $(pwd)/oauth:/app/oauth \
  ghcr.io/chaos7x/yt-upload:latest /usr/local/bin/get_token

Folge den Anweisungen im Terminal, um die Datei `youtube-upload-credentials.json` zu erzeugen[cite: 16, 17].

### 3. Starten via Docker CLI
Starte den Hintergrund-Daemon für die Video-Überwachung[cite: 16]:

docker run -d \
  --name yt-upload \
  --restart unless-stopped \
  -e TZ=Europe/Berlin \
  -e ENABLE_DYNAMIC_PLAYLISTS=true \
  -v $(pwd)/oauth:/app/oauth:rw \
  -v $(pwd)/videos:/videos:rw \
  -v $(pwd)/log:/log:rw \
  ghcr.io/chaos7x/yt-upload:latest

---

## 📦 Docker Compose Integration

Beispielhafte Einbindung über die `docker-compose.yml`[cite: 19]:
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
    env_file:
      - .env
    volumes:
      - ./oauth:/app/oauth:rw
      - ./videos:/videos:rw
      - ./log:/log:rw
```
---

## ✨ Features

* **Inotify-Ordnerüberwachung:** Überwacht das Eingangsverzeichnis `/videos/in` in Echtzeit auf neue Dateien (`.mp4`, `.mkv`, `.mov`, `.m4v`)[cite: 16].
* **Metadaten- & Thumbnail-Extraktion:** Liest Titel, Beschreibung, Genre, Datum und Artist-Tags via FFprobe aus und gewinnt Thumbnails direkt aus MKV-Attachments, MP4-Covern oder Videoframes[cite: 16].
* **Verlustfreies Splitting:** Zertrennt Videos mit einer Laufzeit von über 10 Stunden (36.000 Sekunden) automatisch und ohne Qualitätsverlust in mehrere Segmente (`-c copy`)[cite: 16].
* **Playlist-Verwaltung:** Erstellt und verwaltet Ziel-Playlists dynamisch auf Basis des Artist-Tags oder Fallback-Konfigurationen via Google API Client[cite: 16].
* **Robustes Retry-Handling:** Bietet automatisierte Fehlertoleranz bei Netzwerkabbrüchen und prüft Container-Stabilität/moov-Atome vor dem Upload[cite: 16].

---

## ⚙️ Umgebungsvariablen (Environment)

* `ENABLE_DYNAMIC_PLAYLISTS`: Standard `false`. Aktiviert die automatische Zuweisung von Playlists basierend auf dem `ARTIST`-Tag der Videodatei[cite: 16].
* `TZ`: Zeitzone für Log-Einträge und Zeitstempel (z. B. `Europe/Berlin`)[cite: 16, 19].
* `HOME`: Standard `/tmp` (oder `/app`). Hält Schreibzugriffe aus schreibgeschützten Mounts fern[cite: 19, 23].

---

## 🛠️ Lokaler Build & Entwicklung

Das Image kann über das mitgelieferte Shell-Skript gebaut werden[cite: 20]:

./build.sh v1.0.0

Ein Testlauf oder lokales Debugging des Python-Worker-Skripts ist durch Binden des Skript-Pfads nach `/app/yt-upload` möglich[cite: 18, 19].

---

## 📄 Lizenz

Dieses Projekt steht unter der **GNU General Public License v3.0 (GPLv3)**[cite: 16, 22].

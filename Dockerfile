# ==========================================
# STUFE 0: Statische Binaries bereitstellen
# ==========================================
# Nutzen eines fertigen, statisch kompilierten FFmpeg-Images als Quelle
FROM mwader/static-ffmpeg:latest AS ffmpeg-binaries

# ==========================================
# STUFE 1: BUILDER STAGE (Build-Werkzeuge & Paket-Bau)
# ==========================================
# Leichtgewichtige Debian-Basis für das Bauen von Python-Wheels
FROM debian:trixie-slim AS builder

# Installation von Git und Pip-Tools, um Quellcodes zu klonen und Python-Wheels zu erstellen
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    python3-pip \
    python3-wheel \
    && rm -rf /var/lib/apt/lists/*

# Cache-Buster Argument: Steuert die Git-Version von youtube-upload.
# Wenn sich dieser Wert ändert (z.B. neuer Commit-Hash), bricht Docker den Cache ab hier.
ARG YT_UPLOAD_REF=master

# Erstellen eigenständiger Wheel-Pakete (.whl) für youtube-upload und seine Abhängigkeiten.
# Werden temporär im Ordner /wheels gespeichert.
RUN pip wheel --no-cache-dir --wheel-dir=/wheels \
    google-auth-oauthlib \
    requests-oauthlib \
    git+https://github.com/tokland/youtube-upload.git@${YT_UPLOAD_REF}

# ==========================================
# STUFE 2: FINAL STAGE (Schlankes Laufzeit-Image)
# ==========================================
# Sauberes Ziel-Image ohne temporäre Build-Werkzeuge wie Git
FROM debian:trixie-slim

# Dynamische Build-Argumente (werden beim Build übergeben)
ARG VERSION
ARG BUILD_DATE

# Image-Metadaten (Labels) für Container-Registries und 'docker inspect'
LABEL version="${VERSION}"
LABEL build_date="${BUILD_DATE}"
LABEL maintainer="Chaos7x"
LABEL purpose="YouTube upload automation with ffmpeg + python"

# Kopieren der statisch kompilierten FFmpeg-Binaries aus STUFE 0 direkt nach /usr/local/bin
COPY --from=ffmpeg-binaries /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-binaries /ffprobe /usr/local/bin/ffprobe

# Installation der reinen Laufzeit-Abhängigkeiten (ohne Git!)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcom-err2 \
    mc \
    python3-inotify \
    libimage-exiftool-perl \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Vorbereitete Wheels aus der BUILDER STAGE kopieren, installieren und den /wheels-Ordner sofort löschen
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --break-system-packages /wheels/* && rm -rf /wheels

# Standard-Arbeitsverzeichnis definieren
WORKDIR /app

# Erstellen und Freigeben der Verzeichnisse für Daten und Log-Dateien (Vollzugriff für Container-User)
RUN mkdir /videos && chmod 777 /videos
RUN mkdir /log && chmod 777 /log

# Kopieren der benutzerdefinierten Shell-Skripte und Python-Tools in den System-PFAD (/usr/local/bin)
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY yt-upload.py /usr/local/bin/yt-upload
COPY get_token.py /usr/local/bin/get_token

# Skripte explizit ausführbar machen
RUN chmod +x /usr/local/bin/yt-upload /usr/local/bin/entrypoint.sh /usr/local/bin/get_token

# globale .bashrc im System ablegen und Symlinks anlegen (für interaktive Shells)
COPY bashrc /etc/global.bashrc
RUN ln -s /etc/global.bashrc /tmp/.bashrc
RUN ln -s /etc/global.bashrc /app/.bashrc
ENV HOME=/app

# Startpunkt des Containers festlegen
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

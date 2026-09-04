# ==========================================
# STUFE 0: Statische Binaries bereitstellen
# ==========================================
# Holt die offiziellen, statisch gelinkten FFmpeg- und FFprobe-Binaries von mwader
FROM mwader/static-ffmpeg:latest AS ffmpeg-binaries

# ==========================================
# STUFE 1: FINAL STAGE (Schlankes Laufzeit-Image)
# ==========================================
FROM debian:trixie-slim

ARG VERSION
ARG BUILD_DATE

LABEL version="${VERSION}"
LABEL build_date="${BUILD_DATE}"
LABEL maintainer="Chaos7x"
LABEL purpose="YouTube upload automation with ffmpeg + python"

# ------------------------------------------
# LAYER 1: FFmpeg Binaries aus STUFE 0 kopieren
# ------------------------------------------
COPY --from=ffmpeg-binaries /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-binaries /ffprobe /usr/local/bin/ffprobe

# ------------------------------------------
# LAYER 2: System-Pakete & Python-Bibliotheken in EINEM Rutsch installieren + Aufräumen
# ------------------------------------------
# Installiert Python 3, requests, inotify, ExifTool, Midnight Commander (mc) 
# sowie ca-certificates direkt über den Paketmanager.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-requests \
    python3-inotify \
    libcom-err2 \
    mc \
    libimage-exiftool-perl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Arbeitsverzeichnis & Home-Variable setzen
WORKDIR /app
ENV HOME=/app

# ------------------------------------------
# LAYER 3: Daten- & Log-Verzeichnisse anlegen
# ------------------------------------------
RUN mkdir /videos /log && chmod 777 /videos /log

# ------------------------------------------
# LAYER 4: Lokale Skripte & Configs kopieren
# ------------------------------------------
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY yt-upload.py /usr/local/bin/yt-upload
COPY get_token.py /usr/local/bin/get_token
COPY bashrc /etc/global.bashrc

# ------------------------------------------
# LAYER 5: Rechte setzen & Symlinks anlegen
# ------------------------------------------
RUN chmod +x /usr/local/bin/yt-upload /usr/local/bin/entrypoint.sh /usr/local/bin/get_token \
    && ln -s /etc/global.bashrc /tmp/.bashrc \
    && ln -s /etc/global.bashrc /app/.bashrc

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ==========================================
# STUFE 0: Statische Binaries bereitstellen
# ==========================================
FROM mwader/static-ffmpeg:latest AS ffmpeg-binaries

# ==========================================
# STUFE 1: BUILDER STAGE (Build-Werkzeuge & Paket-Bau)
# ==========================================
FROM debian:trixie-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    python3-pip \
    python3-wheel \
    && rm -rf /var/lib/apt/lists/*

# Cache-Buster Argument für den Git-Commit von youtube-upload
ARG YT_UPLOAD_REF=master

# Erstellen der Wheels für youtube-upload und seine Dependencies
RUN pip wheel --no-cache-dir --wheel-dir=/wheels \
    google-auth-oauthlib \
    requests-oauthlib \
    git+https://github.com/tokland/youtube-upload.git@${YT_UPLOAD_REF}

# ==========================================
# STUFE 2: FINAL STAGE (Schlankes Laufzeit-Image)
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
# LAYER 2: Wheels kopieren
# ------------------------------------------
COPY --from=builder /wheels /wheels

# ------------------------------------------
# LAYER 3: System-Pakete & Pip-Wheels in EINEM Rutsch installieren + Aufräumen
# ------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcom-err2 \
    mc \
    python3-inotify \
    libimage-exiftool-perl \
    python3-pip \
    && pip install --no-cache-dir --break-system-packages /wheels/* \
    && rm -rf /wheels /var/lib/apt/lists/*

# Arbeitsverzeichnis & Home-Variable setzen (Erzeugt 0 MB Daten-Layer)
WORKDIR /app
ENV HOME=/app

# ------------------------------------------
# LAYER 4: Daten- & Log-Verzeichnisse anlegen
# ------------------------------------------
RUN mkdir /videos /log && chmod 777 /videos /log

# ------------------------------------------
# LAYER 5: Lokale Skripte & Configs kopieren
# ------------------------------------------
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY yt-upload.py /usr/local/bin/yt-upload
COPY get_token.py /usr/local/bin/get_token
COPY bashrc /etc/global.bashrc

# ------------------------------------------
# LAYER 6: Rechte setzen & Symlinks anlegen
# ------------------------------------------
RUN chmod +x /usr/local/bin/yt-upload /usr/local/bin/entrypoint.sh /usr/local/bin/get_token \
    && ln -s /etc/global.bashrc /tmp/.bashrc \
    && ln -s /etc/global.bashrc /app/.bashrc

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

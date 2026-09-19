# ==========================================
# STUFE 0: Statische Binaries bereitstellen
# ==========================================
# Holt die offiziellen, statisch gelinkten FFmpeg- und FFprobe-Binaries von mwader
FROM mwader/static-ffmpeg:latest AS ffmpeg-binaries

# ==========================================
# STUFE 1: Builder - installiert das yt_upload-Package isoliert
# ==========================================
# Eigene Stage, damit pip/setuptools NICHT im finalen Laufzeit-Image landen -
# nur das fertig installierte Package wird per COPY --from übernommen.
# Builder- und Final-Stage müssen NICHT exakt dieselbe Python-Minor-Version
# haben - --target liefert reine Python-Dateien ohne C-Extensions, die sind
# über Python-Versionen hinweg portabel (anders als bei --prefix, das über
# versionsspezifische site-packages/dist-packages-Pfade koppelt).
FROM debian:trixie-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-setuptools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml /build/pyproject.toml
COPY src/ /build/src/
# --target statt --prefix: installiert die Library-Dateien FLACH in das
# Zielverzeichnis, ohne sysconfig-spezifische site-packages/dist-packages-
# Verschachtelung. Das war vorher distro-abhängig geraten (Debian patcht auf
# dist-packages + local/-Nesting, Alpine tut das nicht) und ist im Alpine-
# Build tatsächlich fehlgeschlagen - --target umgeht das Problem komplett,
# indem es gar keine Annahme über den Python-Suchpfad-Mechanismus trifft.
# Dafür braucht die finale Stage ein explizites PYTHONPATH (siehe dort).
RUN pip install --break-system-packages --no-deps --no-cache-dir --no-build-isolation --target=/install /build

# ==========================================
# STUFE 2: FINAL STAGE (schlankes Laufzeit-Image, kein pip/setuptools)
# ==========================================
FROM debian:trixie-slim

# ------------------------------------------
# LAYER 1: FFmpeg Binaries aus STUFE 0 kopieren
# ------------------------------------------
COPY --from=ffmpeg-binaries /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-binaries /ffprobe /usr/local/bin/ffprobe

# ------------------------------------------
# LAYER 2: System-Pakete & Python-Bibliotheken in EINEM Rutsch installieren + Aufräumen
# ------------------------------------------
# Installiert Python 3, requests, inotify, ExifTool, Midnight Commander (mc)
# sowie ca-certificates direkt über den Paketmanager. Bewusst OHNE pip/
# setuptools - die werden nur im Builder (STUFE 1) gebraucht.
# apt-get upgrade: das Base-Image selbst (Pakete wie gzip/perl-base/libssl3/
# libsqlite3/libpcre2, die nicht über unsere eigenen apt-get-install-Zeilen
# kommen) hinkt Debians eigenen Security-Patches oft ein paar Tage hinterher,
# bis die Docker-Official-Images-Pipeline es neu baut - ein simples `docker
# pull` holt dann weiterhin die alte, unpatchte Version. apt-get upgrade
# zieht stattdessen bei jedem Build die aktuell in Debians eigenen Repos
# verfügbaren Paketversionen, unabhängig vom Alter des Base-Images selbst.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    python3 \
    python3-requests \
    python3-inotify \
    libcom-err2 \
    mc \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------
# LAYER 2b: Dedizierter Non-Root-User
# ------------------------------------------
# Gehärtetes Image: laeuft standardmaessig nicht als root, auch wenn beim
# Deploy kein `user:`/`-u` gesetzt wird. UID/GID bewusst NICHT fest kodiert -
# useradd/groupadd (statt adduser, das im -slim-Base-Image fehlt und den
# Build mit "exit code: 127" scheitern liess) waehlen automatisch eine freie
# System-UID <1000. Wer die UID an sein eigenes Setup anpassen will (z.B.
# fuer Bind-Mount-Rechte), ueberschreibt sie ganz normal per `docker run -u`/
# Compose `user:` - die 1777-Verzeichnisse unten bleiben davon unabhaengig
# fuer jede UID beschreibbar.
RUN groupadd --system yt-upload \
    && useradd --system --no-create-home --home /nonexistent \
        --shell /usr/sbin/nologin --gid yt-upload yt-upload

# Arbeitsverzeichnis & Home-Variable setzen
WORKDIR /app
ENV HOME=/app

# ------------------------------------------
# LAYER 3: Daten- & Log-Verzeichnisse anlegen
# ------------------------------------------
# 1777 statt 777: die Laufzeit-UID ist unbekannt (frei wählbar via `docker run -u`,
# um Berechtigungskonflikte mit host-gemounteten Verzeichnissen zu vermeiden),
# daher müssen beide Verzeichnisse für jede UID beschreibbar bleiben. Das
# Sticky-Bit (wie bei /tmp) verhindert aber, dass ein Prozess/Nutzer Dateien
# löschen oder umbenennen kann, die ein anderer angelegt hat.
RUN mkdir -p /videos /log /etc/yt-upload/conf.d /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry \
    && chmod 1777 /videos /log /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry

# ------------------------------------------
# LAYER 4: Lokale Skripte, Package & Configs kopieren
# ------------------------------------------
# yt_upload-Package + Entry-Point kommen fertig installiert aus dem Builder
# (STUFE 1) - kein pip-Aufruf mehr in dieser Stage. /install/yt_upload und
# /install/*.dist-info liegen dank --target flach nebeneinander; PYTHONPATH
# zeigt auf deren gemeinsames Elternverzeichnis, damit sowohl `import
# yt_upload` als auch `importlib.metadata.version("yt-upload")` funktionieren.
COPY bashrc /etc/global.bashrc
COPY --chmod=755 src/get_token/get_token.py /usr/local/bin/get_token
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --from=builder /install/bin/yt-upload /usr/local/bin/yt-upload
COPY --from=builder /install/yt_upload /usr/local/lib/yt-upload/yt_upload
COPY --from=builder /install/yt_upload-*.dist-info /usr/local/lib/yt-upload/yt_upload.dist-info
ENV PYTHONPATH=/usr/local/lib/yt-upload
COPY upload.conf.example /etc/yt-upload/upload.conf

# ------------------------------------------
# LAYER 5: Symlinks anlegen
# ------------------------------------------
RUN ln -s /etc/global.bashrc /tmp/.bashrc \
    && ln -s /etc/global.bashrc /app/.bashrc

# /app gehoert dem dedizierten User statt root, damit HOME=/app (siehe oben)
# fuer ihn tatsaechlich beschreibbar ist.
RUN chown yt-upload:yt-upload /app
USER yt-upload

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["/usr/local/bin/yt-upload", "--healthcheck"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

ARG VERSION
ARG BUILD_DATE

LABEL version="${VERSION}"
LABEL build_date="${BUILD_DATE}"
LABEL maintainer="Chaos7x"
LABEL purpose="YouTube upload automation with ffmpeg + python"

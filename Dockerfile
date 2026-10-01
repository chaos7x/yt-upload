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
#
# /rootfs: alles, was die Final-Stage aus dem Repo/Builder braucht, wird hier
# bereits im späteren Ziel-Layout zusammengestellt, damit die Final-Stage es
# mit EINEM einzigen COPY (= einem Layer) statt einem Layer pro Datei übernimmt.
# Die Zielverzeichnisse werden dabei VORHER per mkdir angelegt: COPY --chmod
# würde fehlende Elternverzeichnisse sonst mit demselben Modus anlegen, und
# COPY /rootfs/ / überträgt Verzeichnis-Modi auf das Ziel.
RUN pip install --break-system-packages --no-deps --no-cache-dir --no-build-isolation --target=/install /build \
    && mkdir -p /rootfs/usr/local/bin /rootfs/usr/local/lib/yt-upload /rootfs/etc/yt-upload \
    && mv /install/bin/yt-upload /rootfs/usr/local/bin/ \
    && mv /install/yt_upload /rootfs/usr/local/lib/yt-upload/ \
    && mv /install/yt_upload-*.dist-info /rootfs/usr/local/lib/yt-upload/yt_upload.dist-info
COPY --chmod=755 src/get_token/get_token.py /rootfs/usr/local/bin/get_token
COPY --chmod=755 entrypoint.sh /rootfs/usr/local/bin/entrypoint.sh
COPY bashrc /rootfs/etc/global.bashrc
COPY upload.conf.example /rootfs/etc/yt-upload/upload.conf

# ==========================================
# STUFE 2: FINAL STAGE (schlankes Laufzeit-Image, kein pip/setuptools)
# ==========================================
FROM debian:trixie-slim

# ------------------------------------------
# LAYER 1: FFmpeg Binaries aus STUFE 0 kopieren
# ------------------------------------------
# Bewusst ein eigener Layer VOR allem anderen: die Binaries ändern sich nur
# mit dem mwader-Image, nicht mit jedem Release - der (große) Layer bleibt
# so über Releases hinweg identisch und wird beim Pull nicht neu geladen.
COPY --from=ffmpeg-binaries /ffmpeg /ffprobe /usr/local/bin/

# Arbeitsverzeichnis & Umgebung setzen. PYTHONPATH: /usr/local/lib/yt-upload/
# yt_upload und die *.dist-info liegen dank --target flach nebeneinander;
# PYTHONPATH zeigt auf deren gemeinsames Elternverzeichnis, damit sowohl
# `import yt_upload` als auch `importlib.metadata.version("yt-upload")`
# funktionieren.
WORKDIR /app
ENV HOME=/app \
    PYTHONPATH=/usr/local/lib/yt-upload

# ------------------------------------------
# LAYER 2: System-Pakete, User, Verzeichnisse & Symlinks in EINEM Rutsch
# ------------------------------------------
# Ein einziges RUN statt mehrerer = ein Layer statt fünf.
#
# Pakete: Python 3, requests, Midnight Commander (mc) sowie ca-certificates
# direkt über den Paketmanager. Bewusst OHNE pip/setuptools - die werden nur
# im Builder (STUFE 1) gebraucht.
# apt-get upgrade: das Base-Image selbst (Pakete wie gzip/perl-base/libssl3/
# libsqlite3/libpcre2, die nicht über unsere eigenen apt-get-install-Zeilen
# kommen) hinkt Debians eigenen Security-Patches oft ein paar Tage hinterher,
# bis die Docker-Official-Images-Pipeline es neu baut - ein simples `docker
# pull` holt dann weiterhin die alte, unpatchte Version. apt-get upgrade
# zieht stattdessen bei jedem Build die aktuell in Debians eigenen Repos
# verfügbaren Paketversionen, unabhängig vom Alter des Base-Images selbst.
#
# Non-Root-User: Gehärtetes Image, laeuft standardmaessig nicht als root,
# auch wenn beim Deploy kein `user:`/`-u` gesetzt wird. UID/GID bewusst NICHT
# fest kodiert - useradd/groupadd (statt adduser, das im -slim-Base-Image
# fehlt und den Build mit "exit code: 127" scheitern liess) waehlen
# automatisch eine freie System-UID <1000. Wer die UID an sein eigenes Setup
# anpassen will (z.B. fuer Bind-Mount-Rechte), ueberschreibt sie ganz normal
# per `docker run -u`/Compose `user:` - die 1777-Verzeichnisse bleiben davon
# unabhaengig fuer jede UID beschreibbar.
#
# 1777 statt 777: die Laufzeit-UID ist unbekannt (frei wählbar via `docker run -u`,
# um Berechtigungskonflikte mit host-gemounteten Verzeichnissen zu vermeiden),
# daher müssen die Verzeichnisse für jede UID beschreibbar bleiben. Das
# Sticky-Bit (wie bei /tmp) verhindert aber, dass ein Prozess/Nutzer Dateien
# löschen oder umbenennen kann, die ein anderer angelegt hat.
#
# /app gehoert dem dedizierten User statt root, damit HOME=/app (siehe oben)
# fuer ihn tatsaechlich beschreibbar ist.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    python3 \
    python3-requests \
    libcom-err2 \
    mc \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system yt-upload \
    && useradd --system --no-create-home --home /nonexistent \
        --shell /usr/sbin/nologin --gid yt-upload yt-upload \
    && mkdir -p /log /etc/yt-upload/conf.d /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry \
    && chmod 1777 /log /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry \
    && ln -s /etc/global.bashrc /tmp/.bashrc \
    && ln -s /etc/global.bashrc /app/.bashrc \
    && chown yt-upload:yt-upload /app

# ------------------------------------------
# LAYER 3: Package, Skripte & Configs aus dem Builder übernehmen
# ------------------------------------------
# yt_upload-Package + Entry-Point, get_token, entrypoint.sh, bashrc und
# upload.conf liegen in /rootfs (STUFE 1) bereits im Ziel-Layout - ein COPY,
# ein Layer, kein pip-Aufruf in dieser Stage. Steht bewusst NACH dem RUN oben,
# damit eine reine Code-Änderung den Paket-Layer aus dem Cache weiterverwendet.
COPY --from=builder /rootfs/ /

USER yt-upload

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["/usr/local/bin/yt-upload", "--healthcheck"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

ARG VERSION
ARG BUILD_DATE

LABEL version="${VERSION}" \
      build_date="${BUILD_DATE}" \
      maintainer="Chaos7x" \
      purpose="YouTube upload automation with ffmpeg + python"

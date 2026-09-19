#!/bin/sh
# Wird von dpkg nach dem Entpacken des yt-upload-daemon .deb ausgefuehrt
# (fpm --after-install). Das eigentliche yt-upload-Package (CLI) hat kein
# eigenes postinst - Systemuser und systemd-Service sind reine
# Daemon-Modus-Angelegenheiten, get-token laeuft ohnehin nie als Service.
set -e

if ! getent passwd yt-upload >/dev/null 2>&1; then
    adduser --system --group --no-create-home --home /nonexistent \
        --shell /usr/sbin/nologin yt-upload
fi

# Gemeinsame Gruppe fuer die Uebergabeverzeichnisse der Pipeline
# (tw-recorder -> fetchbridge -> yt-upload). Jedes der drei .deb-Pakete legt
# Gruppe und Verzeichnisse unabhaengig und idempotent an, da die Installationsreihenfolge
# nicht garantiert ist. 2775 + setgid, damit jedes Mitglied neu angelegte
# Dateien des jeweils anderen Dienstes auch wieder verschieben/loeschen kann
# (dafuer reicht Schreibrecht auf das Verzeichnis, unabhaengig vom Datei-Owner).
if ! getent group media-pipeline >/dev/null 2>&1; then
    addgroup --system media-pipeline
fi
adduser yt-upload media-pipeline

mkdir -p /srv/media-pipeline/recordings /srv/media-pipeline/incoming
chown root:media-pipeline /srv/media-pipeline /srv/media-pipeline/recordings /srv/media-pipeline/incoming
chmod 2775 /srv/media-pipeline /srv/media-pipeline/recordings /srv/media-pipeline/incoming

# WORK_DIR/DONE_DIR/CORRUPT_DIR/RETRY_DIR sind rein interner Zustand von
# yt-upload (kein anderer Dienst liest/schreibt dort) - eigenes, privates
# Verzeichnis statt der gemeinsamen Gruppe von oben.
mkdir -p /srv/yt-upload/work /srv/yt-upload/done /srv/yt-upload/corrupt /srv/yt-upload/retry
chown -R yt-upload:yt-upload /srv/yt-upload
chmod -R 0750 /srv/yt-upload

# /run/systemd/system existiert nur, wenn systemd tatsaechlich als Init-System
# laeuft (nicht z.B. in einem Chroot/Container-Build ohne systemd) - ohne
# diese Absicherung wuerde die Paketinstallation dort fehlschlagen.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi

echo ""
echo "yt-upload-daemon wurde installiert, der systemd-Service ist aber noch NICHT aktiviert."
echo "Bitte zuerst [paths] in /etc/yt-upload/upload.conf setzen und einmalig"
echo "'get-token' ausfuehren, dann den Dienst manuell aktivieren und starten:"
echo ""
echo "    systemctl enable --now yt-upload"
echo ""

exit 0

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

mkdir -p /srv/media-pipeline/recordings /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry
chown root:media-pipeline /srv/media-pipeline /srv/media-pipeline/recordings /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry
chmod 2775 /srv/media-pipeline /srv/media-pipeline/recordings /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry

# /run/systemd/system existiert nur, wenn systemd tatsaechlich als Init-System
# laeuft (nicht z.B. in einem Chroot/Container-Build ohne systemd) - ohne
# diese Absicherung wuerde die Paketinstallation dort fehlschlagen. Auf einem
# Nicht-systemd-Host (Devuan, Debian mit sysvinit-core) wird stattdessen das
# mitgelieferte /etc/init.d/yt-upload per update-rc.d registriert - beide
# Zweige schliessen sich damit gegenseitig aus, es wird nie beides parallel
# verwaltet.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
elif command -v update-rc.d >/dev/null 2>&1; then
    update-rc.d yt-upload defaults >/dev/null
fi

echo ""
echo "yt-upload-daemon wurde installiert, der Dienst ist aber noch NICHT aktiviert."
echo "Bitte zuerst [paths] in /etc/yt-upload/upload.conf setzen und einmalig"
echo "'get-token' ausfuehren, dann den Dienst manuell aktivieren und starten:"
echo ""
if [ -d /run/systemd/system ]; then
    echo "    systemctl enable --now yt-upload"
else
    echo "    service yt-upload start"
fi
echo ""
echo "Optional: liegen gebliebene Dateien in RETRY_DIR (z.B. nach einem"
echo "quotaExceeded) taeglich automatisch zurueck nach IN_DIR verschieben:"
echo ""
if [ -d /run/systemd/system ]; then
    echo "    systemctl enable --now yt-upload-retry.timer"
else
    echo "    cp /etc/yt-upload/yt-upload-retry.cron.example /etc/cron.d/yt-upload-retry"
fi
echo ""

exit 0

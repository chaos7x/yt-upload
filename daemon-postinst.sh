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

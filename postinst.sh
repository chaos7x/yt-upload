#!/bin/sh
# Wird von dpkg nach dem Entpacken des .deb ausgefuehrt (fpm --after-install).
# Betrifft nur den Daemon-Modus (yt-upload.service) - get-token ist ein
# einmaliges, interaktives Setup-Tool und laeuft nie als Systemd-Service.
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

exit 0

#!/bin/sh
# Wird von dpkg vor dem Entfernen des yt-upload-daemon .deb ausgefuehrt (fpm --before-remove).
# $1 = "remove" beim tatsaechlichen Entfernen, "upgrade" bei einem Update auf
# eine neue Version - der Dienst soll bei einem Upgrade weiterlaufen.
set -e

if [ "$1" = "remove" ]; then
    if [ -d /run/systemd/system ]; then
        systemctl stop yt-upload.service || true
        systemctl disable yt-upload.service || true
        systemctl stop yt-upload-retry.timer || true
        systemctl disable yt-upload-retry.timer || true
    elif [ -x /etc/init.d/yt-upload ]; then
        # /etc/cron.d/yt-upload-retry ist ein --config-files-Conffile - dpkg
        # kuemmert sich beim Entfernen/Purge selbst darum, hier kein manuelles rm noetig.
        /etc/init.d/yt-upload stop || true
        command -v update-rc.d >/dev/null 2>&1 && update-rc.d yt-upload remove >/dev/null || true
    fi
fi

exit 0

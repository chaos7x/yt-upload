#!/bin/sh
# Wird von dpkg nach dem Entfernen des Pakets ausgefuehrt (fpm --after-remove).
set -e

if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi

exit 0

#!/bin/bash

# Wenn das Skript von außen (über das Volume) reingereicht wird, nutzen wir das
if [ -f /app/yt-upload ]; then
    echo ">>> Using external yt-upload from volume..."
    chmod +x /app/yt-upload 2>/dev/null || true
    exec /app/yt-upload
else
    # Andernfalls greifen wir auf das im Image eingebaute Skript zurück
    echo ">>> Using internal /usr/local/bin/yt-upload from image..."
    exec /usr/local/bin/yt-upload
fi

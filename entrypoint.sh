#!/bin/sh

# Prüfen, ob ein gültiger Systembefehl oder absoluter Pfad als erstes Argument übergeben wurde
if [ "$#" -gt 0 ] && ( [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 ); then
    exec "$@"
fi

# Docker startet den Recorder standardmäßig im Daemon-Modus, wenn keine Argumente übergeben werden
if [ "$#" -eq 0 ]; then
    set -- -D
fi

# Wenn das Skript von außen (über das Volume) reingereicht wird, nutzen wir
# das - aber nur, wenn es nicht gruppen-/weltweit beschreibbar ist. Ohne diese
# Prüfung würde JEDE Datei, die es irgendwie schafft, unter /app/yt-upload zu
# landen (z.B. ein falsch konfiguriertes/kompromittiertes Volume), bei jedem
# Container-Neustart automatisch mit vollen Container-Rechten ausgeführt. Die
# Prüfung läuft über python3 (immer im Image vorhanden) statt über `stat`,
# dessen Flags sich zwischen GNU/coreutils (Debian/Dockerfile.pyimg) und
# BusyBox (Alpine) unterscheiden.
if [ -f /app/yt-upload ]; then
    if python3 -c "
import os, stat, sys
st = os.stat('/app/yt-upload')
sys.exit(1 if stat.S_IMODE(st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH) else 0)
"; then
        echo ">>> Using external yt-upload from volume..."
        chmod +x /app/yt-upload 2>/dev/null || true
        exec /app/yt-upload "$@"
    else
        echo ">>> WARNING: /app/yt-upload exists but is group- or world-writable - refusing to execute it for safety. Falling back to internal script." >&2
    fi
fi

# Andernfalls (kein externes Skript, oder oben abgelehnt) greifen wir auf das
# im Image eingebaute Skript zurück
echo ">>> Using internal /usr/local/bin/yt-upload from image..."
exec /usr/local/bin/yt-upload "$@"

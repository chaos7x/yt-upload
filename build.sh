#!/usr/bin/env bash
set -e

# Version aus erstem Argument übernehmen oder Fallback auf 'dev'
VERSION="${1:-dev}"
BUILD_DATE="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# echo "Kompiliere yt-upload.py (Version: $VERSION)..."
# python3 -c "import py_compile; py_compile.compile('yt-upload.pyc', cfile='yt-upload.py', doraise=True)"

echo "Baue Docker-Image (Version: $VERSION)..."

# Tag-Liste initialisieren
TAGS=(-t "yt-upload:$VERSION")

# Tag 'latest' nur hinzufügen, wenn es sich NICHT um ein 'dev'-Build handelt
if [ "$VERSION" != "dev" ]; then
  TAGS+=(-t "yt-upload:latest")
fi

docker build -f Dockerfile.python \
  --build-arg VERSION="$VERSION" \
  --build-arg BUILD_DATE="$BUILD_DATE" \
  --pull \
  "${TAGS[@]}" .

# Lokales temporäres Artefakt aufräumen
# rm yt-upload
echo "Fertig!"

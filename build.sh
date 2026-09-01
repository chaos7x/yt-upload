#!/usr/bin/env bash
set -e

# Argumente verarbeiten:
# $1 = Version (Standard: 'dev')
# $2 = Target ('local' oder 'registry', Standard: 'local')
VERSION="${1:-dev}"
TARGET="${2:-local}"

BUILD_DATE="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
LOCAL_PREFIX="yt-upload"
REGISTRY_PREFIX="ghcr.io/chaos7x/${LOCAL_PREFIX}"

# Bestimmen, welcher Präfix genutzt wird
if [ "$TARGET" = "registry" ]; then
  IMAGE_NAME="$REGISTRY_PREFIX"
else
  IMAGE_NAME="$LOCAL_PREFIX"
fi

# echo "Kompiliere yt-upload.py (Version: $VERSION)..."
# python3 -c "import py_compile; py_compile.compile('yt-upload.pyc', cfile='yt-upload.py', doraise=True)"

echo "Baue Docker-Image (Version: $VERSION, Ziel: $TARGET)..."

# Tag-Liste initialisieren
TAGS=(-t "$IMAGE_NAME:$VERSION")

# Tag 'latest' nur hinzufügen, wenn es sich NICHT um ein 'dev'-Build handelt
if [ "$VERSION" != "dev" ]; then
  TAGS+=(-t "$IMAGE_NAME:latest")
fi

# Docker Build ausführen
docker build -f Dockerfile.python \
  --build-arg VERSION="$VERSION" \
  --build-arg BUILD_DATE="$BUILD_DATE" \
  --pull \
  "${TAGS[@]}" .

# Falls als Ziel 'registry' übergeben wurde, Images pushen
if [ "$TARGET" = "registry" ]; then
  echo "Veröffentliche auf GitHub Container Registry..."
  docker push "$IMAGE_NAME:$VERSION"

  if [ "$VERSION" != "dev" ]; then
    docker push "$IMAGE_NAME:latest"
  fi
fi

# Lokales temporäres Artefakt aufräumen
# rm yt-upload
echo "Fertig!"

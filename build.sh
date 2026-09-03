#!/usr/bin/env bash
set -e

VERSION="${1:-dev}"
IMAGE_NAME="yt-upload"
BUILD_DATE="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

echo "🔍 Prüfe neuesten Commit von tokland/youtube-upload..."
YT_UPLOAD_REF="$(git ls-remote https://github.com/tokland/youtube-upload.git HEAD | cut -f1)"
echo "📌 Ref: ${YT_UPLOAD_REF}"

echo "🔨 Baue lokales Docker-Image (${IMAGE_NAME}:${VERSION})..."

docker build -f Dockerfile \
  --build-arg VERSION="$VERSION" \
  --build-arg BUILD_DATE="$BUILD_DATE" \
  --build-arg YT_UPLOAD_REF="$YT_UPLOAD_REF" \
  -t "${IMAGE_NAME}:${VERSION}" .

echo "🎉 Build erfolgreich abgeschlossen!"

# Prüfen, ob die Override-Datei für lokale Dev-Builds vorhanden ist
if [ -f "docker-compose.override.yml" ] || [ -f "docker-compose.override.yaml" ]; then
  echo "💡 'docker compose up -d' nutzt jetzt deine lokale 'docker-compose.override.yml'."
else
  echo "⚠️  HINWEIS: Keine 'docker-compose.override.yml' gefunden!"
  echo "   'docker compose up -d' zieht ohne Override das Image aus der Registry (GHCR)."
  echo "   Passe ggf. deine .env an oder erstelle eine 'docker-compose.override.y(a)ml'."
fi

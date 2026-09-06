#!/usr/bin/env bash
set -e

# Argumente intelligent parsen:
# - Keine Argumente: VERSION="dev", VARIANT=""
# - Ein Argument "pyimg": VERSION="dev", VARIANT="pyimg"
# - Ein Argument (sonstiges): VERSION="$1", VARIANT=""
# - Zwei Argumente: VERSION="$1", VARIANT="$2"
VERSION="dev"
VARIANT=""

if [ -n "${1:-}" ]; then
    if [ "$1" = "pyimg" ]; then
        VARIANT="pyimg"
    else
        VERSION="$1"
        VARIANT="${2:-}"
    fi
fi

IMAGE_NAME="yt-upload"
BUILD_DATE="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Bestimme Dockerfile und Tag dynamisch basierend auf der Variante
if [ -n "$VARIANT" ]; then
    DOCKERFILE="Dockerfile.${VARIANT}"
    IMAGE_TAG="${IMAGE_NAME}:${VERSION}-${VARIANT}"
else
    DOCKERFILE="Dockerfile"
    IMAGE_TAG="${IMAGE_NAME}:${VERSION}"
fi

echo "🔨 Baue lokales Docker-Image (${IMAGE_TAG}) mit ${DOCKERFILE}..."

docker build -f "$DOCKERFILE" \
  --build-arg VERSION="$VERSION" \
  --build-arg BUILD_DATE="$BUILD_DATE" \
  --build-arg YT_UPLOAD_REF="$YT_UPLOAD_REF" \
  -t "${IMAGE_TAG}" .

echo "🎉 Build erfolgreich abgeschlossen!"

# Prüfen, ob die Override-Datei für lokale Dev-Builds vorhanden ist
if [ -f "docker-compose.override.yml" ] || [ -f "docker-compose.override.yaml" ]; then
  echo "💡 'docker compose up -d' nutzt jetzt deine lokale 'docker-compose.override.yml'."
else
  echo "⚠️  HINWEIS: Keine 'docker-compose.override.yml' gefunden!"
  echo "    'docker compose up -d' zieht ohne Override das Image aus der Registry (GHCR)."
  echo "    Passe ggf. deine .env an oder erstelle eine 'docker-compose.override.y(a)ml'."
fi

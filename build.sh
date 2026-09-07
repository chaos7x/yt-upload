#!/usr/bin/env bash
set -e

VERSION="dev"
VARIANT=""

# Bekannte Varianten definieren (für die automatische Erkennung bei nur 1 Argument)
KNOWN_VARIANTS=("pyimg" "alpine")

is_known_variant() {
    local target="$1"
    for v in "${KNOWN_VARIANTS[@]}"; do
        if [ "$v" = "$target" ]; then
            return 0
        fi
    done
    return 1
}

case "$#" in
    0)
        # Keine Argumente -> Defaults bleiben (VERSION="dev", VARIANT="")
        ;;
    1)
        # Ein Argument: Prüfen, ob es eine Variante oder eine Version ist
        if is_known_variant "$1"; then
            VARIANT="$1"
        else
            VERSION="$1"
        fi
        ;;
    2)
        # Zwei Argumente: Klar definiert als Version und Variante
        VERSION="$1"
        VARIANT="$2"
        ;;
    *)
        echo "❌ Zu viele Argumente. Verwendung: $0 [VERSION] [VARIANT]" >&2
        exit 1
        ;;
esac

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

if [ -f "docker-compose.override.yml" ] || [ -f "docker-compose.override.yaml" ]; then
  echo "💡 'docker compose up -d' nutzt jetzt deine lokale 'docker-compose.override.yml'."
else
  echo "⚠️  HINWEIS: Keine 'docker-compose.override.yml' gefunden!"
  echo "    'docker compose up -d' zieht ohne Override das Image aus der Registry (GHCR)."
  echo "    Passe ggf. deine .env an oder erstelle eine 'docker-compose.override.y(a)ml'."
fi

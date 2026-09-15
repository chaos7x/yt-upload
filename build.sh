#!/usr/bin/env bash
set -e

# Name & Standard-Version aus pyproject.toml lesen, statt sie hier zusätzlich
# zu pflegen. tomllib ist Standardbibliothek seit Python 3.11 (passt zu
# requires-python in der pyproject.toml), keine zusätzliche Abhängigkeit.
read_pyproject_field() {
    python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(data['project']['$1'])
"
}

if [ ! -f "pyproject.toml" ]; then
    echo "❌ pyproject.toml nicht gefunden - bitte aus dem Repo-Root ausführen." >&2
    exit 1
fi

IMAGE_NAME="$(read_pyproject_field name)"
VERSION="$(read_pyproject_field version)"
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
        # Keine Argumente -> Defaults bleiben (VERSION aus pyproject.toml, VARIANT="")
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

docker build --pull -f "$DOCKERFILE" \
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

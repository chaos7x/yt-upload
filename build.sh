#!/usr/bin/env bash
set -e

# Argumente verarbeiten:
# $1 = Version (Standard: 'dev')
# $2 = Target ('local' oder 'registry', Standard: 'local')
VERSION="${1:-dev}"
TARGET="${2:-local}"

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Wenn es sich nicht um ein Dev-Tag handelt, Prüfungen für Release durchführen
if [ "$VERSION" != "dev" ]; then
  echo "🔍 Prüfe Release-Voraussetzungen für Version '$VERSION'..."

  # 1. Check: Befinden wir uns auf main?
  if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "❌ FEHLER: Releases dürfen nur auf dem 'main'-Branch gebaut werden!"
    echo "Aktueller Branch: '$CURRENT_BRANCH'"
    exit 1
  fi

  # 2. Check: Gibt es uncommitted Änderungen?
  if [ -n "$(git status --porcelain)" ]; then
    echo "⚠️ WARNUNG: Du hast uncommitted Änderungen auf deinem Branch."
    read -p "Möchtest du trotzdem fortfahren? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      echo "Abgebrochen."
      exit 1
    fi
  fi
fi

BUILD_DATE="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
LOCAL_PREFIX="yt-upload"
REGISTRY_PREFIX="ghcr.io/chaos7x/${LOCAL_PREFIX}"

LOCAL_IMAGE="$LOCAL_PREFIX:$VERSION"
REGISTRY_IMAGE="$REGISTRY_PREFIX:$VERSION"

# Prüfen, ob das Image lokal bereits existiert (gilt NICHT für 'dev'-Builds)
if [ "$VERSION" != "dev" ] && docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
  echo "ℹ️ Lokales Release-Image '$LOCAL_IMAGE' wurde gefunden (Build wird übersprungen)."

  # Falls das Ziel 'registry' ist, das lokale Image für GHCR retaggen
  if [ "$TARGET" = "registry" ]; then
    echo "🔗 Verlinke (tagge) lokales Image für die Registry..."
    docker tag "$LOCAL_IMAGE" "$REGISTRY_IMAGE"
    docker tag "$LOCAL_IMAGE" "$REGISTRY_PREFIX:latest"
  fi
else
  # Image existiert nicht ODER es ist eine 'dev'-Version -> Docker Build erzwingen
  if [ "$TARGET" = "registry" ]; then
    IMAGE_NAME="$REGISTRY_PREFIX"
  else
    IMAGE_NAME="$LOCAL_PREFIX"
  fi

  echo "🔨 Baue Docker-Image (Version: $VERSION, Ziel: $TARGET, Branch: $CURRENT_BRANCH)..."

  TAGS=(-t "$IMAGE_NAME:$VERSION")

  if [ "$VERSION" != "dev" ]; then
    TAGS+=(-t "$IMAGE_NAME:latest")
  fi

  docker build -f Dockerfile \
    --build-arg VERSION="$VERSION" \
    --build-arg BUILD_DATE="$BUILD_DATE" \
    --pull \
    "${TAGS[@]}" .
fi

# Falls als Ziel 'registry' übergeben wurde, Images pushen
if [ "$TARGET" = "registry" ]; then
  echo "🚀 Veröffentliche auf GitHub Container Registry..."
  docker push "$REGISTRY_IMAGE"

  if [ "$VERSION" != "dev" ]; then
    docker push "$REGISTRY_PREFIX:latest"

    # Optionalen Git-Tag erzeugen (nur bei Registry-Releases auf main)
    read -p "Möchtest du auch den Git-Tag 'v${VERSION}' erstellen und pushen? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
      if git rev-parse "v${VERSION}" >/dev/null 2>&1; then
        echo "⚠️ Git-Tag 'v${VERSION}' existiert bereits lokal."
      else
        git tag -a "v${VERSION}" -m "Release v${VERSION}"
        git push origin "v${VERSION}"
        echo "✅ Git-Tag 'v${VERSION}' erfolgreich gepusht!"
      fi
    fi
  fi
fi

echo "🎉 Fertig!"

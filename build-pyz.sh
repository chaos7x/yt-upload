#!/usr/bin/env bash
set -e

# Baut ein einzelnes, selbst-enthaltenes yt-upload.pyz aus src/yt_upload/ -
# inklusive requests und (optional) inotify, damit die Datei auf jedem
# System mit nacktem `python3` läuft, ganz ohne apt/pip-Vorbereitung.
#
# WICHTIG: src/yt_upload/ bleibt die einzige Quelle der Wahrheit - dieses
# Skript baut die .pyz bei jedem Lauf frisch daraus, nichts wird hier von
# Hand gepflegt oder dupliziert.

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

# "name" in pyproject.toml ist der Distributionsname (Bindestriche erlaubt,
# z.B. "yt-upload") - das tatsächliche Package-Verzeichnis unter src/ muss
# aber ein gültiger Python-Bezeichner sein und kann davon abweichen
# (yt-upload -> yt_upload). Daher hier automatisch ermitteln statt aus dem
# Namen abzuleiten.
DIST_NAME="$(read_pyproject_field name)"
PACKAGE_DIR=""
for d in src/*/; do
    if [ -f "${d}__init__.py" ]; then
        PACKAGE_DIR="${d%/}"
        break
    fi
done
if [ -z "$PACKAGE_DIR" ]; then
    echo "❌ Kein Package mit __init__.py unter src/ gefunden." >&2
    exit 1
fi
PACKAGE_NAME="$(basename "$PACKAGE_DIR")"

OUTPUT="${DIST_NAME}.pyz"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

# Prüft, ob überhaupt ein "daemon"-Extra in [project.optional-dependencies]
# existiert, statt es hart zu kodieren - nicht jedes der drei Projekte hat
# eins (tw-recorder z.B. hat gar keine Dependencies).
HAS_DAEMON_EXTRA="$(python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
extras = data.get('project', {}).get('optional-dependencies', {})
print('yes' if 'daemon' in extras else 'no')
")"
if [ "$HAS_DAEMON_EXTRA" = "yes" ]; then
    PIP_TARGET_SPEC=".[daemon]"
else
    PIP_TARGET_SPEC="."
fi

echo "🔨 Baue ${OUTPUT} aus src/${PACKAGE_NAME}/..."

mkdir -p "$BUILD_DIR/src"

# WICHTIG: Kein manuelles `cp -r src/${PACKAGE_NAME}` mehr vorher - das war
# überflüssig und hat mit dem folgenden `pip install .` kollidiert (pip
# installiert das eigene Package ja ohnehin aus der pyproject.toml, inkl.
# korrekter dist-info). Der doppelte Schritt führte zu einer
# "Target directory already exists"-Warnung, bei der pip die eigene
# Installation übersprungen hat - inklusive der dist-info, die
# importlib.metadata.version() zur Laufzeit braucht.
pip install --no-cache-dir --target="$BUILD_DIR/src" "$PIP_TARGET_SPEC" --quiet

# WICHTIG: Nur fremde dist-info-Ordner (requests, inotify, ...) löschen,
# NICHT die des eigenen Packages - importlib.metadata.version() in
# yt_upload/__init__.py braucht genau diese, um die Version zur Laufzeit
# aufzulösen. Ein pauschales `find -name "*.dist-info" -delete` hatte hier
# genau das mit gelöscht, was gebraucht wird (Version fiel auf den
# "local-inst"-Fallback zurück).
find "$BUILD_DIR" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.dist-info" ! -name "${PACKAGE_NAME}-*.dist-info" -exec rm -rf {} + 2>/dev/null || true
# bin/ (von pip generierte Entry-Point-Skripte) wird nicht gebraucht - der
# tatsächliche Einstiegspunkt kommt über zipapps eigenes -m/__main__.py.
rm -rf "$BUILD_DIR/src/bin"

python3 -m zipapp "$BUILD_DIR/src" -o "$OUTPUT" -p "/usr/bin/env python3" -m "${PACKAGE_NAME}.main:main"
chmod +x "$OUTPUT"

echo "🎉 ${OUTPUT} gebaut ($(du -h "$OUTPUT" | cut -f1))"
echo "   Test: ./${OUTPUT} --version"

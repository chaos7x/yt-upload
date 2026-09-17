#!/usr/bin/env bash
set -e

# Baut ein selbst-enthaltenes .pyz pro Eintrag in [project.scripts] (aktuell
# yt-upload.pyz und get-token.pyz) - inklusive requests und (optional)
# inotify, damit die Dateien auf jedem System mit nacktem `python3` laufen,
# ganz ohne apt/pip-Vorbereitung.
#
# WICHTIG: src/ bleibt die einzige Quelle der Wahrheit - dieses Skript baut
# die .pyz-Dateien bei jedem Lauf frisch daraus, nichts wird hier von Hand
# gepflegt oder dupliziert. Welche Skripte gebaut werden und welchen
# Einstiegspunkt sie nutzen, wird direkt aus [project.scripts] gelesen statt
# aus einer Annahme über die Verzeichnisstruktur unter src/ (z.B. "erstes
# Package mit __init__.py, Einstiegspunkt ist <package>.main:main") - das
# brach beim Hinzufügen des zweiten Packages (get_token, kein main.py,
# alphabetisch vor yt_upload einsortiert).

if [ ! -f "pyproject.toml" ]; then
    echo "❌ pyproject.toml nicht gefunden - bitte aus dem Repo-Root ausführen." >&2
    exit 1
fi

# "name" in pyproject.toml ist der Distributionsname (Bindestriche erlaubt,
# z.B. "yt-upload") - wird nur für den dist-info-Namensfilter unten gebraucht,
# nicht mehr für den .pyz-Dateinamen (siehe SCRIPTS unten). pip normalisiert
# Bindestriche im dist-info-Ordnernamen selbst zu Unterstrichen
# (yt-upload -> yt_upload-1.2.3.dist-info), daher hier ebenfalls ersetzen -
# sonst hält der Filter unten das eigene dist-info fälschlich für "fremd"
# und löscht es mit, wodurch importlib.metadata.version() zur Laufzeit auf
# den "local-inst"-Fallback zurückfällt.
DIST_NAME="$(python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(data['project']['name'])
")"
DIST_NAME_NORMALIZED="${DIST_NAME//-/_}"

# Liest [project.scripts] als "script-name<TAB>modul.pfad:funktion"-Zeilen -
# exakt dieselben Einträge, die auch `pip install .` als CLI-Befehle anlegt.
SCRIPTS="$(python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
for name, target in data.get('project', {}).get('scripts', {}).items():
    print(f'{name}\t{target}')
")"
if [ -z "$SCRIPTS" ]; then
    echo "❌ Kein Eintrag unter [project.scripts] in pyproject.toml gefunden." >&2
    exit 1
fi

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

echo "🔨 Installiere ${PIP_TARGET_SPEC} nach ${BUILD_DIR}/src..."
mkdir -p "$BUILD_DIR/src"

# WICHTIG: Kein manuelles `cp -r src/<package>` mehr vorher - das war
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
find "$BUILD_DIR" -name "*.dist-info" ! -name "${DIST_NAME_NORMALIZED}-*.dist-info" -exec rm -rf {} + 2>/dev/null || true
# bin/ (von pip generierte Entry-Point-Skripte) wird nicht gebraucht - der
# tatsächliche Einstiegspunkt kommt über zipapps eigenes -m/__main__.py.
rm -rf "$BUILD_DIR/src/bin"

# Beide Packages liegen im selben installierten Baum, daher wird für jeden
# Script-Eintrag ein eigenes .pyz aus demselben $BUILD_DIR/src gebaut, nur
# mit jeweils anderem zipapp-Einstiegspunkt (-m). Etwas redundanter Inhalt
# pro Datei (z.B. yt_upload im get-token.pyz) wird bewusst in Kauf genommen,
# damit jede .pyz weiterhin vollständig eigenständig lauffähig bleibt.
while IFS=$'\t' read -r script_name entry_point; do
    [ -z "$script_name" ] && continue
    OUTPUT="${script_name}.pyz"
    echo "🔨 Baue ${OUTPUT} (Einstiegspunkt ${entry_point})..."
    python3 -m zipapp "$BUILD_DIR/src" -o "$OUTPUT" -p "/usr/bin/env python3" -m "$entry_point"
    chmod +x "$OUTPUT"
    echo "🎉 ${OUTPUT} gebaut ($(du -h "$OUTPUT" | cut -f1))"
done <<< "$SCRIPTS"

echo "   Test: ./yt-upload.pyz --version"

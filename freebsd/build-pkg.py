#!/usr/bin/env python3
"""Baut FreeBSD-Pakete (.pkg) ohne FreeBSD-Host, z.B. auf dem Ubuntu-Runner.

Ein FreeBSD-Paket ist ein tar.xz mit +COMPACT_MANIFEST/+MANIFEST (JSON) vor
den eigentlichen Dateien. fpm (-t freebsd) kann das grundsätzlich auch,
verwirft aber --depends und --config-files komplett und übernimmt die
Datei-Owner des Build-Users - daher dieses kleine, abhängigkeitsfreie Skript.

Pro FreeBSD-Major-Version (OS_VERSIONS) entsteht ein eigenes Paket, weil pkg
die Major-Version im ABI-Feld exakt prüft ("FreeBSD:14:*" lässt sich auf 15
nicht ohne IGNORE_OSVERSION installieren).

Python-Pakete landen in einem eigenen Verzeichnis unter /usr/local/lib/<name>
statt im versionsabhängigen site-packages, die Befehle in /usr/local/bin sind
kleine Wrapper, die dieses Verzeichnis in sys.path eintragen. Interpreter und
Python-Abhängigkeiten sind bewusst auf dieselbe Python-Version (PY_FLAVOR)
festgelegt, damit nicht z.B. py311-requests neben einem python3 -> 3.12
installiert wird und der Import dann trotzdem fehlschlägt.

Aufruf: python3 freebsd/build-pkg.py <version> [ausgabeverzeichnis]
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

# --- projektspezifisch ------------------------------------------------------
PY_FLAVOR = "311"  # an FreeBSDs Standard-Python (DEFAULT_VERSIONS python) anpassen
OS_VERSIONS = ["14", "15"]
MAINTAINER = "chaos7x"
WWW = "https://github.com/chaos7x/yt-upload"
LIB_DIR = "/usr/local/lib/yt-upload"
EXAMPLES_DIR = "/usr/local/share/examples/yt-upload"

PACKAGES = [
    {
        "name": "yt-upload",
        "comment": "Automatisierter YouTube Upload Workflow (CLI)",
        "deps": {
            f"python{PY_FLAVOR}": "lang/python" + PY_FLAVOR,
            f"py{PY_FLAVOR}-requests": "www/py-requests",
            "ffmpeg": "multimedia/ffmpeg",
        },
        "python": True,
        "files": {
            "upload.conf.example": f"{EXAMPLES_DIR}/upload.conf.example",
        },
        "post_install": """\
mkdir -p /etc/yt-upload/conf.d
if [ ! -e /etc/yt-upload/upload.conf ]; then
    cp {examples}/upload.conf.example /etc/yt-upload/upload.conf
fi
""",
        "message": "Konfiguration: /etc/yt-upload/upload.conf (Vorlage unter "
                   f"{EXAMPLES_DIR}). Einmalig 'get-token' ausfuehren. Fuer den "
                   "Daemon-Modus zusaetzlich das Paket yt-upload-daemon installieren.",
    },
    {
        "name": "yt-upload-daemon",
        "comment": "rc.d-Dienst fuer den yt-upload-Daemon-Modus (yt-upload -D)",
        "deps": {"yt-upload": f"{MAINTAINER}/yt-upload"},
        "python": False,
        "files": {
            "freebsd/rc.d/yt-upload": "/usr/local/etc/rc.d/yt-upload",
        },
        "modes": {"/usr/local/etc/rc.d/yt-upload": 0o755},
        "post_install": """\
pw groupshow media-pipeline >/dev/null 2>&1 || pw groupadd media-pipeline
if ! pw usershow yt-upload >/dev/null 2>&1; then
    pw useradd yt-upload -c "yt-upload daemon" -d /nonexistent -s /usr/sbin/nologin -G media-pipeline
fi
for dir in /srv/media-pipeline /srv/media-pipeline/recordings /srv/media-pipeline/incoming /srv/media-pipeline/work /srv/media-pipeline/done /srv/media-pipeline/corrupt /srv/media-pipeline/retry; do
    if [ ! -d "$dir" ]; then
        install -d -o root -g media-pipeline -m 2775 "$dir"
    fi
done
""",
        "message": "Der Dienst ist NICHT aktiviert. Nach Konfiguration und get-token:\n"
                   "    sysrc yt_upload_enable=YES\n"
                   "    service yt-upload start\n"
                   "Optionaler taeglicher Retry-Requeue:\n"
                   "    echo '0 3 * * * yt-upload /usr/local/bin/yt-upload --requeue-retries "
                   ">/dev/null 2>&1' > /usr/local/etc/cron.d/yt-upload-retry",
    },
]
# ----------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WRAPPER = """#!/usr/local/bin/python{dotted}
import sys
sys.path.insert(0, "{lib_dir}")
from {module} import {func}
sys.exit({func}())
"""


def _stage_python(stage):
    """Installiert das Projekt per pip nach LIB_DIR und legt die Wrapper an."""
    lib = stage + LIB_DIR
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
         "--no-compile", "--target", lib, ROOT],
        check=True,
    )
    # pip legt unter --target auch bin/ an - dort stünde der Interpreter-Pfad
    # dieses Build-Hosts drin, die eigenen Wrapper unten ersetzen das.
    shutil.rmtree(os.path.join(lib, "bin"), ignore_errors=True)
    for dirpath, dirnames, _ in os.walk(lib):
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(dirpath, "__pycache__"))

    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]
    dotted = f"{PY_FLAVOR[0]}.{PY_FLAVOR[1:]}"
    os.makedirs(stage + "/usr/local/bin", exist_ok=True)
    for cmd, target in scripts.items():
        module, func = target.split(":")
        path = f"{stage}/usr/local/bin/{cmd}"
        with open(path, "w") as f:
            f.write(WRAPPER.format(dotted=dotted, lib_dir=LIB_DIR, module=module, func=func))
        os.chmod(path, 0o755)


def _stage_files(stage, pkg):
    for src, dst in pkg["files"].items():
        os.makedirs(os.path.dirname(stage + dst), exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, src), stage + dst)
        os.chmod(stage + dst, pkg.get("modes", {}).get(dst, 0o644))


def _manifest(pkg, version, osver, stage):
    files = {}
    for dirpath, _, filenames in os.walk(stage):
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            files["/" + os.path.relpath(full, stage)] = "1$" + digest
    manifest = {
        "name": pkg["name"],
        "origin": f"{MAINTAINER}/{pkg['name']}",
        "version": version,
        "comment": pkg["comment"],
        "desc": pkg["comment"],
        "maintainer": MAINTAINER,
        "www": WWW,
        "abi": f"FreeBSD:{osver}:*",
        "arch": f"freebsd:{osver}:*",
        "prefix": "/usr/local",
        "licenselogic": "single",
        "licenses": ["GPLv3"],
        # pkg löst Abhängigkeiten über den Namen auf, die Version dient nur der
        # Anzeige - "0" heißt "beliebige Version aus dem Repo".
        "deps": {dep: {"origin": origin, "version": "0"} for dep, origin in pkg["deps"].items()},
    }
    compact = dict(manifest)
    if pkg.get("post_install"):
        manifest["scripts"] = {"post-install": pkg["post_install"].format(examples=EXAMPLES_DIR)}
    if pkg.get("message"):
        manifest["messages"] = [{"message": pkg["message"]}]
    manifest["files"] = files
    return compact, manifest


def _tar_filter(info, name):
    # tarfile entfernt führende "/" aus dem Archivnamen, FreeBSD-Pakete
    # speichern aber absolute Pfade (passend zu den +MANIFEST-Einträgen).
    info.name = name
    info.uid = info.gid = 0
    info.uname, info.gname = "root", "wheel"
    return info


def build(version, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    built = []
    for pkg in PACKAGES:
        with tempfile.TemporaryDirectory() as stage:
            if pkg["python"]:
                _stage_python(stage)
            _stage_files(stage, pkg)
            for osver in OS_VERSIONS:
                compact, manifest = _manifest(pkg, version, osver, stage)
                out = os.path.join(out_dir, f"{pkg['name']}-{version}-freebsd{osver}.pkg")
                with tempfile.TemporaryDirectory() as meta:
                    for fname, data in (("+COMPACT_MANIFEST", compact), ("+MANIFEST", manifest)):
                        with open(os.path.join(meta, fname), "w") as f:
                            json.dump(data, f, indent=1)
                    with tarfile.open(out, "w:xz") as tar:
                        for fname in ("+COMPACT_MANIFEST", "+MANIFEST"):
                            tar.add(os.path.join(meta, fname), fname,
                                    filter=lambda i, n=fname: _tar_filter(i, n))
                        for path in sorted(manifest["files"]):
                            tar.add(stage + path, path, recursive=False,
                                    filter=lambda i, n=path: _tar_filter(i, n))
                built.append(out)
    return built


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for path in build(sys.argv[1].lstrip("v"), sys.argv[2] if len(sys.argv) > 2 else "."):
        print(path)

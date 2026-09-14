"""
Gemeinsame Test-Infrastruktur für yt-upload.py.

yt-upload.py hat einen Bindestrich im Dateinamen und ist damit kein gültiger
Python-Modulname (ein normales `import yt-upload` ist nicht möglich). Wir laden
die Datei deshalb dynamisch über importlib - das funktioniert unabhängig vom
Dateinamen und braucht keine Umbenennung des Produktionscodes.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "yt-upload.py"


def _load_yt_upload_module():
    spec = importlib.util.spec_from_file_location("yt_upload", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["yt_upload"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def yt_upload():
    """Lädt yt-upload.py einmal pro Test-Session und stellt es als Modul-Objekt bereit."""
    return _load_yt_upload_module()

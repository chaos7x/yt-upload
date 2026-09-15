"""
Gemeinsame Test-Infrastruktur für das yt_upload-Package (src/yt_upload/).

Seit dem Package-Split ist yt_upload ein normaler, gültiger Python-Paketname
(kein Bindestrich mehr wie bei der alten yt-upload.py) - daher jetzt normale
`import`-Statements statt des früheren importlib-Loader-Workarounds.

Eine Fixture pro Submodul, statt einer einzigen "yt_upload"-Fixture: das
Package ist bewusst in mehrere Module mit klaren Verantwortlichkeiten
aufgeteilt (config, text_utils, fileutils, media, youtube_api, pipeline,
daemon, logging_setup, healthcheck) - Tests sollen genau das Modul
referenzieren, das die getestete Funktion tatsächlich enthält, statt über
eine künstliche Kompatibilitätsfassade drüberzupatchen.

WICHTIG für monkeypatch.setattr(...)-Aufrufe: Config-Werte (z.B. ALLOW_OVERWRITE,
WORK_DIR) leben ausschließlich in config.py. Andere Module referenzieren sie zur
Laufzeit als `config.NAME` (nie als eigene Kopie) - Tests, die solche Werte
patchen wollen, müssen daher IMMER die config-Fixture verwenden, auch wenn die
eigentlich getestete Funktion in einem anderen Modul liegt (siehe
test_path_resolution.py, test_uploader_regressions.py).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

GET_TOKEN_PATH = REPO_ROOT / "get_token.py"


@pytest.fixture(scope="session")
def yt_upload_pkg():
    """Das Top-Level-Package selbst (__title__, __version__)."""
    import yt_upload
    return yt_upload


@pytest.fixture(scope="session")
def config():
    from yt_upload import config
    return config


@pytest.fixture(scope="session")
def text_utils():
    from yt_upload import text_utils
    return text_utils


@pytest.fixture(scope="session")
def fileutils():
    from yt_upload import fileutils
    return fileutils


@pytest.fixture(scope="session")
def media():
    from yt_upload import media
    return media


@pytest.fixture(scope="session")
def youtube_api():
    from yt_upload import youtube_api
    return youtube_api


@pytest.fixture(scope="session")
def pipeline():
    from yt_upload import pipeline
    return pipeline


@pytest.fixture(scope="session")
def daemon():
    from yt_upload import daemon
    return daemon


@pytest.fixture(scope="session")
def logging_setup():
    from yt_upload import logging_setup
    return logging_setup


@pytest.fixture(scope="session")
def healthcheck():
    from yt_upload import healthcheck
    return healthcheck


@pytest.fixture(autouse=True)
def reset_config_after_test():
    """
    Setzt veränderte config-Werte nach JEDEM Test zurück, damit direkte
    Zuweisungen (z.B. in test_uploader_regressions.py, das noch
    unittest.mock.patch statt pytest monkeypatch nutzt) sich nicht in
    nachfolgende Tests durchschleppen. monkeypatch.setattr() räumt sich zwar
    selbst auf, dieser Schutz greift primär als zusätzliches Sicherheitsnetz
    für den unittest-basierten Test.
    """
    from yt_upload import config as _config
    original = dict(vars(_config))
    yield
    for key, value in original.items():
        setattr(_config, key, value)


def _load_module_from_path(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def get_token_module():
    """
    get_token.py ist weiterhin ein eigenständiges Skript (nicht Teil des
    yt_upload-Package-Splits) und behält daher den importlib-Loader.
    """
    return _load_module_from_path("get_token", GET_TOKEN_PATH)

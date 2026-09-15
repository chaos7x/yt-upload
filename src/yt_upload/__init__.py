"""yt-upload: Automatisierter YouTube Upload Workflow (CLI, Auto-Batch, Daemon)."""

from importlib.metadata import PackageNotFoundError, version

__title__ = "YouTube Video Uploader & CLI-Uploader"

try:
    # Funktioniert, sobald das Package per `pip install .` installiert wurde
    # (Bare-Metal-Installation) - liest die Version dann aus den von pip
    # erzeugten Metadaten, statt sie hier ein zweites Mal zu pflegen.
    __version__ = version("yt-upload")
except PackageNotFoundError:
    # Docker-Image: Package wird nur kopiert (siehe bin/yt-upload-Shim),
    # nicht per pip installiert - dort existieren keine pip-Metadaten.
    __version__ = "1.2.2"

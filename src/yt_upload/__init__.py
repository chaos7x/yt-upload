"""yt-upload: Automatisierter YouTube Upload Workflow (CLI, Auto-Batch, Daemon)."""

from importlib.metadata import PackageNotFoundError, version

__title__ = "YouTube Video Uploader & CLI-Uploader"

try:
    # Funktioniert bei jeder Installationsart: echtes `pip install .`
    # (Bare-Metal), `pip install --target=...` (Dockerfile/Dockerfile.alpine)
    # oder `pip install .[daemon]` (Dockerfile.pyimg) - liest die Version aus
    # den von pip erzeugten Metadaten, statt sie hier ein zweites Mal zu pflegen.
    __version__ = version("yt-upload")
except PackageNotFoundError:
    # Nur relevant, wenn die .py-Dateien ganz ohne pip-Installation direkt
    # kopiert/ausgeführt werden (z.B. schneller lokaler Test) - keiner der
    # aktuell gepflegten Dockerfiles/Installationswege braucht diesen Zweig.
    __version__ = "local-inst"

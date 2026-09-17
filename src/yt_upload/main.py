"""CLI-Einstiegspunkt: Argument-Parsing und Moduswahl (Datei/Auto-Batch/Dämon)."""

import argparse
import logging
import os
import sys

from yt_upload import __title__, __version__, config
from yt_upload.daemon import acquire_instance_lock, find_existing_video, run_daemon
from yt_upload.fileutils import ensure_directories
from yt_upload.healthcheck import run_healthcheck
from yt_upload.logging_setup import setup_logging
from yt_upload.pipeline import process_single_file

logger = logging.getLogger(__name__)


def _parse_bool(value):
    """argparse-Typ-Konverter für explizite Boolean-CLI-Werte (--embeddable=true/false)."""
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"Ungültiger Boolean-Wert: '{value}' (erwartet: true/false)")


def parse_arguments(argv=None):
    """Initialisiert das Parsing der Kommandozeilenargumente.

    argv=None (Standard) lässt argparse wie gewohnt sys.argv[1:] lesen; ein
    expliziter Wert (z.B. in Tests) überschreibt das, ohne sys.argv patchen
    zu müssen.
    """
    parser = argparse.ArgumentParser(
        description=f"{__title__} v{__version__}",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("file", nargs="?", help="Pfad zur hochzuladenden Videodatei (im manuellen Modus)")
    parser.add_argument("-a", "--auto", action="store_true", help="Automatischer Batch-Modus für ein Verzeichnis")
    parser.add_argument("-D", "--daemon", action="store_true", help="Dämon-Modus: Dauerhafte inotify-Verzeichnisüberwachung")
    parser.add_argument("--healthcheck", action="store_true", help="Prüft nur den Heartbeat des laufenden Dämons und beendet sich sofort (für Docker HEALTHCHECK)")

    parser.add_argument("-t", "--title", help="Video-Titel (Standard: Metadaten/Dateiname)")
    desc_group = parser.add_mutually_exclusive_group()
    desc_group.add_argument("-d", "--description", help="Video-Beschreibung")
    desc_group.add_argument("--description-file", help="Pfad zu einer Textdatei mit der Video-Beschreibung (alternativ zu -d/--description)")
    parser.add_argument("-c", "--category", help="Kategorie ID oder Name (z.B. Entertainment, Gaming, 22)")
    parser.add_argument("-v", "--version", action="version", version=f"{__title__} v{__version__}")
    parser.add_argument("--tags", help="Kommagetrennte Liste von Tags")
    
    parser.add_argument("--privacy", choices=["public", "private", "unlisted"], default=None, help="Sichtbarkeit")

    parser.add_argument("--thumbnail", help="Pfad zu benutzerdefiniertem Thumbnail-Bild")
    parser.add_argument("--playlist", help="Name der Ziel-Playlist")
    parser.add_argument("--publish-at", help="Geplante Veröffentlichung (ISO-Format 8601)")
    parser.add_argument("--license", choices=["youtube", "creativeCommon"], default=None, help="Videolizenz")
    parser.add_argument("--location", help="Geo-Koordinaten (Format: 'latitude=50.9,longitude=6.9')")
    parser.add_argument("--recording-date", help="Aufnahmedatum")

    parser.add_argument("--default-language", default=None, help="Standardsprache des Titels/der Beschreibung")
    parser.add_argument("--default-audio-language", default=None, help="Standardsprache des Audios")
    parser.add_argument("--embeddable", type=_parse_bool, default=None, metavar="{true,false}", help="Einbetten auf externen Seiten erlauben oder verbieten (true/false)")

    parser.add_argument("--credentials-file", default=None, help="Pfad zur OAuth Credentials JSON")
    parser.add_argument("--client-secrets", help="Pfad zur Google Client Secrets JSON")
    parser.add_argument("--chunksize", type=int, default=268435456, help="Upload Chunk-Größe in Bytes")
    parser.add_argument("--open-link", action="store_true", help="Nach Upload Video-URL im Standardbrowser öffnen")

    return parser.parse_args(argv)


def _apply_description_file(args):
    """
    Ersetzt args.description durch den Inhalt von args.description_file, falls
    gesetzt (mutually exclusive mit -d/--description, siehe parse_arguments()).
    Bricht mit sys.exit(1) ab, wenn die angegebene Datei nicht existiert.
    """
    if not args.description_file:
        return
    if not os.path.exists(args.description_file):
        logger.error(f"Angegebene Beschreibungsdatei existiert nicht: {args.description_file}")
        sys.exit(1)
    with open(args.description_file, "r", encoding="utf-8") as f:
        args.description = f.read()


def main():
    """Hauptablaufsteuerung abhängig von den übergebenen Parametern."""
    # --healthcheck wird bewusst vor jeglicher Config-/Verzeichnis-/Logging-
    # Initialisierung behandelt: der Aufruf soll schnell sein und keine
    # Nebenwirkungen haben, da er typischerweise alle paar Sekunden von einem
    # externen Healthcheck (Docker HEALTHCHECK, Kubernetes livenessProbe)
    # ausgeführt wird.
    args = parse_arguments()
    if args.healthcheck:
        sys.exit(run_healthcheck())

    config.load_configuration(log_changes=False)
    ensure_directories()

    setup_logging()

    logger.info(f"=== {__title__} v{__version__} gestartet ===")

    if not acquire_instance_lock():
        sys.exit(1)

    # Modus 1: Manueller Upload einer angegebenen Datei
    if args.file:
        if not os.path.exists(args.file):
            logger.error(f"Angegebene Datei existiert nicht: {args.file}")
            sys.exit(1)

        _apply_description_file(args)
        process_single_file(args.file, args)

    # Modus 2: Auto-Batch – verarbeitet alle bereits vorhandenen Dateien nacheinander
    elif args.auto:
        logger.info(f"Starte einmalige Batch-Verarbeitung in {config.IN_DIR}...")
        while True:
            file_to_process = find_existing_video(config.IN_DIR)
            if not file_to_process:
                logger.info("Keine weiteren Dateien im Eingangsverzeichnis gefunden.")
                break
            process_single_file(file_to_process, args=None)

    # Modus 3: Dämonen-Modus – Dauerhafte Überwachung mittels Inotify
    elif args.daemon:
        run_daemon()

    else:
        print(f"{__title__} v{__version__}\n")
        print("Bitte einen Betriebsmodus wählen:")
        print("  - Einzelne Datei:  yt-upload /pfad/zum/video.mp4")
        print("  - Auto-Pipeline:   yt-upload -a")
        print("  - Dämon-Modus:     yt-upload -D")
        print("\nNutze -h oder --help für alle Optionen.")


if __name__ == "__main__":
    main()

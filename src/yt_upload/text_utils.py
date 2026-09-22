"""
Reine Text-/Metadaten-Hilfsfunktionen: Titel, Kategorie-Mapping, Datumsformate,
Sanitizing und Zensur.
"""

import logging
import re
import unicodedata
from datetime import UTC, datetime

from yt_upload import config

logger = logging.getLogger(__name__)


def truncate_title(title: str, max_length: int = 100) -> str:
    """Kürzt Titel unter Einhaltung des YouTube-Limits von 100 Zeichen."""
    if not title:
        return ""
    if len(title) <= max_length:
        return title
    return title[: max_length - 3].rstrip() + "..."


def get_valid_category_id(category_input):
    """Mappt Kategorie-Namen auf die offiziellen numerischen YouTube-IDs."""
    if not category_input:
        return "22"

    if str(category_input).isdigit():
        return str(category_input)

    cat_lower = str(category_input).lower()
    mapping = {
        "film & animation": "1",
        "autos & vehicles": "2",
        "music": "10",
        "pets & animals": "15",
        "sports": "17",
        "short movies": "18",
        "travel & events": "19",
        "gaming": "20",
        "videoblogging": "21",
        "people & blogs": "22",
        "comedy": "23",
        "entertainment": "24",
        "news & politics": "25",
        "howto & style": "26",
        "education": "27",
        "science & technology": "28",
        "nonprofits & activism": "29",
        "movies": "30",
        "anime/animation": "31",
        "action/adventure": "32",
        "classics": "33",
        "documentary": "35",
        "drama": "36",
        "family": "37",
        "foreign": "38",
        "horror": "39",
        "sci-fi/fantasy": "40",
        "thriller": "41",
        "shorts": "42",
        "shows": "43",
        "trailers": "44",
    }

    for key, val in mapping.items():
        if key in cat_lower:
            return val

    return "22"


def normalize_recording_date(value):
    """Konvertiert Datumsangaben in das ISO-8601 UTC-Format für die API."""
    if not value:
        return None

    value = str(value).strip()

    if re.fullmatch(r"\d{8}", value):
        parsed = datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
        return parsed.isoformat().replace("+00:00", "Z")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
        return parsed.isoformat().replace("+00:00", "Z")

    return value


def sanitize_text(text):
    """
    Entfernt Steuerzeichen und ungültige Klammern (<, >) für die YouTube API,
    bewahrt aber bewusst Zeilenumbrüche (\\n) - mehrzeilige Beschreibungen
    (z.B. von yt-dlp mit --embed-metadata erzeugte Video-Beschreibungen mit
    Absätzen/Links) würden sonst zu einem einzigen Textblock zusammengequetscht.
    unicodedata.category('\\n') ist 'Cc' (Steuerzeichen) und würde ohne
    Sonderbehandlung mitentfernt.
    """
    if not text:
        return text

    # Kurzform <3 in Herz umwandeln
    text = text.replace("<3", "♥")

    # Spitzzeichen für die API entfernen
    text = text.replace("<", "").replace(">", "")

    # Steuerzeichen entfernen (NUL, BEL etc.), \n dabei ausdrücklich erhalten
    cleaned_chars = [c for c in text if c == '\n' or unicodedata.category(c) != 'Cc']
    result = ''.join(cleaned_chars)

    # Nur horizontalen Whitespace (Leerzeichen/Tabs/\r) pro Zeile zusammenfassen -
    # \s+ würde \n mit erfassen und die Zeilenumbrüche doch wieder zerstören.
    lines = [re.sub(r'[^\S\n]+', ' ', line).strip() for line in result.split('\n')]
    return '\n'.join(lines).strip()


def parse_location(location_str):
    """Verarbeitet Geokoordinaten aus String-Formaten (latitude=X,longitude=Y)."""
    if not location_str:
        return None
    try:
        parts = dict(item.split('=') for item in location_str.split(','))
        loc = {
            "latitude": float(parts["latitude"]),
            "longitude": float(parts["longitude"])
        }
        if "altitude" in parts:
            loc["altitude"] = float(parts["altitude"])
        return loc
    except (ValueError, KeyError, TypeError) as e:
        # Rohe Koordinaten sind Standortdaten und dürfen laut Logging-Policy nur
        # auf DEBUG-Level erscheinen, nie auf WARNING/INFO oder höher.
        logger.warning(f"Konnte Location-String nicht parsen: {e}")
        logger.debug(f"Fehlerhafter Location-String war: '{location_str}'")
        return None


def censor_text(text: str) -> str:
    """
    Durchsucht den übergebenen Text nach Wörtern aus der Konfigurations-Blacklist
    und ersetzt gefundene Treffer durch Sternchen (*), unter Beibehaltung der Wortlänge.
    """
    if not config.ENABLE_DESCRIPTION_CENSOR or not text:
        return text

    if config.DESCRIPTION_BLACKLIST:
        escaped_words = [re.escape(word) for word in config.DESCRIPTION_BLACKLIST if word.strip()]
        if escaped_words:
            pattern = re.compile(r'(?i)' + '|'.join(escaped_words))

            def replace_match(match):
                matched_str = match.group(0)
                return '*' * len(matched_str)

            text = pattern.sub(replace_match, text)

    return text


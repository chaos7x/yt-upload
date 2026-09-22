"""Tests für sanitize_text, truncate_title, normalize_recording_date, get_valid_category_id."""


class TestSanitizeText:
    def test_removes_angle_brackets(self, text_utils):
        # sanitize_text entfernt nur die Zeichen < und >, keine "Tags" im HTML-Sinn
        assert text_utils.sanitize_text("Hello <b>World</b>") == "Hello bWorld/b"

    def test_converts_heart_shortcode(self, text_utils):
        assert text_utils.sanitize_text("I <3 you") == "I ♥ you"

    def test_removes_control_characters(self, text_utils):
        assert text_utils.sanitize_text("Hello\x00World") == "HelloWorld"

    def test_collapses_horizontal_whitespace(self, text_utils):
        assert text_utils.sanitize_text("Hello   \t  World") == "Hello World"

    def test_preserves_newlines(self, text_utils):
        """
        Regression: unicodedata.category('\\n') ist 'Cc' (Steuerzeichen) und
        wurde deshalb faelschlich mitentfernt, das anschliessende \\s+ haette
        verbleibende Umbrueche zusaetzlich zu einem Leerzeichen kollabiert -
        eine mehrzeilige Video-Beschreibung (z.B. von yt-dlp erzeugt, mit
        Absaetzen zwischen Werbung/Links/Quellen) wurde dadurch beim Upload zu
        einem einzigen Textblock zusammengequetscht.
        """
        assert text_utils.sanitize_text("Hello   \n\t  World") == "Hello\nWorld"

    def test_preserves_paragraph_breaks_and_trims_each_line(self, text_utils):
        multiline = "Zeile 1  \n\n  Zeile 2\t\nZeile 3   "
        assert text_utils.sanitize_text(multiline) == "Zeile 1\n\nZeile 2\nZeile 3"

    def test_empty_string_stays_empty(self, text_utils):
        assert text_utils.sanitize_text("") == ""

    def test_none_passthrough(self, text_utils):
        # sanitize_text gibt bei falsy Input den Input unverändert zurück (kein Crash)
        assert text_utils.sanitize_text(None) is None


class TestTruncateTitle:
    def test_short_title_unchanged(self, text_utils):
        assert text_utils.truncate_title("Kurzer Titel") == "Kurzer Titel"

    def test_empty_title(self, text_utils):
        assert text_utils.truncate_title("") == ""

    def test_long_title_truncated_to_100_chars(self, text_utils):
        long_title = "A" * 150
        result = text_utils.truncate_title(long_title)
        assert len(result) == 100
        assert result.endswith("...")

    def test_custom_max_length(self, text_utils):
        result = text_utils.truncate_title("A" * 20, max_length=10)
        assert len(result) == 10
        assert result.endswith("...")


class TestNormalizeRecordingDate:
    def test_yyyymmdd_format(self, text_utils):
        assert text_utils.normalize_recording_date("20250101") == "2025-01-01T00:00:00Z"

    def test_iso_date_format(self, text_utils):
        assert text_utils.normalize_recording_date("2025-01-01") == "2025-01-01T00:00:00Z"

    def test_unrecognized_format_passthrough(self, text_utils):
        # Format, das keinem der beiden Muster entspricht, wird unverändert zurückgegeben
        assert text_utils.normalize_recording_date("not-a-date") == "not-a-date"

    def test_none_returns_none(self, text_utils):
        assert text_utils.normalize_recording_date(None) is None

    def test_empty_string_returns_none(self, text_utils):
        assert text_utils.normalize_recording_date("") is None


class TestGetValidCategoryId:
    def test_numeric_string_passthrough(self, text_utils):
        assert text_utils.get_valid_category_id("20") == "20"

    def test_known_category_name_case_insensitive(self, text_utils):
        assert text_utils.get_valid_category_id("Gaming") == "20"

    def test_none_defaults_to_people_and_blogs(self, text_utils):
        assert text_utils.get_valid_category_id(None) == "22"

    def test_unknown_category_defaults_to_people_and_blogs(self, text_utils):
        assert text_utils.get_valid_category_id("Foobar") == "22"

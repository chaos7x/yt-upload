"""Tests für sanitize_text, truncate_title, normalize_recording_date, get_valid_category_id."""


class TestSanitizeText:
    def test_removes_angle_brackets(self, yt_upload):
        # sanitize_text entfernt nur die Zeichen < und >, keine "Tags" im HTML-Sinn
        assert yt_upload.sanitize_text("Hello <b>World</b>") == "Hello bWorld/b"

    def test_converts_heart_shortcode(self, yt_upload):
        assert yt_upload.sanitize_text("I <3 you") == "I ♥ you"

    def test_removes_control_characters(self, yt_upload):
        assert yt_upload.sanitize_text("Hello\x00World") == "HelloWorld"

    def test_collapses_whitespace(self, yt_upload):
        assert yt_upload.sanitize_text("Hello   \n\t  World") == "Hello World"

    def test_empty_string_stays_empty(self, yt_upload):
        assert yt_upload.sanitize_text("") == ""

    def test_none_passthrough(self, yt_upload):
        # sanitize_text gibt bei falsy Input den Input unverändert zurück (kein Crash)
        assert yt_upload.sanitize_text(None) is None


class TestTruncateTitle:
    def test_short_title_unchanged(self, yt_upload):
        assert yt_upload.truncate_title("Kurzer Titel") == "Kurzer Titel"

    def test_empty_title(self, yt_upload):
        assert yt_upload.truncate_title("") == ""

    def test_long_title_truncated_to_100_chars(self, yt_upload):
        long_title = "A" * 150
        result = yt_upload.truncate_title(long_title)
        assert len(result) == 100
        assert result.endswith("...")

    def test_custom_max_length(self, yt_upload):
        result = yt_upload.truncate_title("A" * 20, max_length=10)
        assert len(result) == 10
        assert result.endswith("...")


class TestNormalizeRecordingDate:
    def test_yyyymmdd_format(self, yt_upload):
        assert yt_upload.normalize_recording_date("20250101") == "2025-01-01T00:00:00Z"

    def test_iso_date_format(self, yt_upload):
        assert yt_upload.normalize_recording_date("2025-01-01") == "2025-01-01T00:00:00Z"

    def test_unrecognized_format_passthrough(self, yt_upload):
        # Format, das keinem der beiden Muster entspricht, wird unverändert zurückgegeben
        assert yt_upload.normalize_recording_date("not-a-date") == "not-a-date"

    def test_none_returns_none(self, yt_upload):
        assert yt_upload.normalize_recording_date(None) is None

    def test_empty_string_returns_none(self, yt_upload):
        assert yt_upload.normalize_recording_date("") is None


class TestGetValidCategoryId:
    def test_numeric_string_passthrough(self, yt_upload):
        assert yt_upload.get_valid_category_id("20") == "20"

    def test_known_category_name_case_insensitive(self, yt_upload):
        assert yt_upload.get_valid_category_id("Gaming") == "20"

    def test_none_defaults_to_people_and_blogs(self, yt_upload):
        assert yt_upload.get_valid_category_id(None) == "22"

    def test_unknown_category_defaults_to_people_and_blogs(self, yt_upload):
        assert yt_upload.get_valid_category_id("Foobar") == "22"

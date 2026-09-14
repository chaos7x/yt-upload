"""Tests für parse_location (Geokoordinaten-Parsing)."""


class TestParseLocation:
    def test_basic_coordinates(self, yt_upload):
        result = yt_upload.parse_location("latitude=52.5,longitude=13.4")
        assert result == {"latitude": 52.5, "longitude": 13.4}

    def test_coordinates_with_altitude(self, yt_upload):
        result = yt_upload.parse_location("latitude=1,longitude=2,altitude=3")
        assert result == {"latitude": 1.0, "longitude": 2.0, "altitude": 3.0}

    def test_malformed_string_returns_none(self, yt_upload):
        # Kein "key=value"-Format -> darf nicht crashen, sondern None liefern
        assert yt_upload.parse_location("banana") is None

    def test_missing_required_key_returns_none(self, yt_upload):
        assert yt_upload.parse_location("longitude=13.4") is None

    def test_non_numeric_value_returns_none(self, yt_upload):
        assert yt_upload.parse_location("latitude=abc,longitude=13.4") is None

    def test_none_input_returns_none(self, yt_upload):
        assert yt_upload.parse_location(None) is None

    def test_empty_string_returns_none(self, yt_upload):
        assert yt_upload.parse_location("") is None

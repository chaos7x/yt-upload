"""Tests für load_configuration() (config.py) - Config-Datei-Parsing-Robustheit."""


def _write_config(tmp_path, content):
    path = tmp_path / "upload.conf"
    path.write_text(content, encoding="utf-8")
    return str(path)


class TestLoadConfigurationParsingErrors:
    """
    Anders als fetchbridge/tw-recorder nutzt yt-upload KEIN
    allow_no_value=True - ein Schlüssel ohne "= wert" (z.B. vergessenes
    "= Entertainment") lässt configparser.read() deshalb direkt mit einem
    ParsingError fehlschlagen, statt wie dort einen stillen None-Wert zu
    liefern. Ungefangen würde das den ganzen Dämon abstürzen lassen -
    derselbe grundsätzliche Fehlerfall (kaputte Config crasht den Prozess),
    der in tw-recorder für dessen [channels]-Sektion gefixt wurde.
    """

    def test_malformed_config_line_does_not_crash(self, config, tmp_path, monkeypatch):
        conf_path = _write_config(tmp_path, "[settings]\ndefault_category\n")
        empty_conf_d = tmp_path / "conf.d"
        empty_conf_d.mkdir()

        monkeypatch.setattr(config, "CONF_PATH", conf_path)
        monkeypatch.setattr(config, "CONF_D_DIR", str(empty_conf_d))

        config.load_configuration()  # darf nicht mit ParsingError crashen

    def test_valid_config_still_applies_after_a_previous_parsing_error(self, config, tmp_path, monkeypatch):
        """
        Stellt sicher, dass der try/except um config.read() den Fehler nur
        abfängt und loggt, statt z.B. versehentlich das Neuladen komplett
        zu deaktivieren - ein Folgeaufruf mit gültiger Config muss wieder
        normal funktionieren.
        """
        conf_path = _write_config(tmp_path, "[settings]\ndefault_category\n")
        empty_conf_d = tmp_path / "conf.d"
        empty_conf_d.mkdir()
        monkeypatch.setattr(config, "CONF_PATH", conf_path)
        monkeypatch.setattr(config, "CONF_D_DIR", str(empty_conf_d))
        monkeypatch.delenv("DEFAULT_CATEGORY", raising=False)
        config.load_configuration()

        _write_config(tmp_path, "[settings]\ndefault_category = Gaming\n")
        config.load_configuration()

        assert config.DEFAULT_CATEGORY == "Gaming"

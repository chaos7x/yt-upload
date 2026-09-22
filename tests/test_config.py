"""Tests für load_configuration(), _resolve_log_level_name() und _resolve_credentials_file() (config.py)."""


def _write_config(tmp_path, content):
    path = tmp_path / "upload.conf"
    path.write_text(content, encoding="utf-8")
    return str(path)


class TestResolveLogLevelName:
    def test_defaults_to_info_when_not_debug(self, config, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)

        assert config._resolve_log_level_name(debug_mode=False) == "INFO"

    def test_debug_mode_true_maps_to_debug(self, config, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)

        assert config._resolve_log_level_name(debug_mode=True) == "DEBUG"

    def test_log_level_env_var_is_used(self, config, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "warning")

        assert config._resolve_log_level_name(debug_mode=False) == "WARNING"

    def test_explicit_log_level_takes_precedence_over_debug_mode(self, config, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "ERROR")

        assert config._resolve_log_level_name(debug_mode=True) == "ERROR"

    def test_invalid_log_level_falls_back_to_default(self, config, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "not-a-real-level")

        assert config._resolve_log_level_name(debug_mode=False) == "INFO"
        assert config._resolve_log_level_name(debug_mode=True) == "DEBUG"


class TestResolveCredentialsFile:
    """
    Bare-Metal-Default ist /etc/yt-upload/ (derselbe Ort wie upload.conf),
    nicht BASE_DIR (Installationsort des Python-Packages) - dort war die
    Datei für den dedizierten yt-upload-Systemuser weder zuverlässig
    auffindbar noch beschreibbar, und das README beschrieb fälschlich
    /app/oauth/... auch für Bare-Metal (das ist nur der Container-Default).
    """

    def test_bare_metal_default_is_etc_yt_upload(self, config, monkeypatch):
        monkeypatch.delenv("CREDENTIALS_FILE", raising=False)

        assert config._resolve_credentials_file(in_container=False) == "/etc/yt-upload/youtube-upload-credentials.json"

    def test_container_default_is_app_oauth(self, config, monkeypatch):
        monkeypatch.delenv("CREDENTIALS_FILE", raising=False)

        assert config._resolve_credentials_file(in_container=True) == "/app/oauth/youtube-upload-credentials.json"

    def test_explicit_env_var_takes_precedence(self, config, monkeypatch):
        monkeypatch.setenv("CREDENTIALS_FILE", "/custom/path.json")

        assert config._resolve_credentials_file(in_container=False) == "/custom/path.json"
        assert config._resolve_credentials_file(in_container=True) == "/custom/path.json"


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

    def test_broken_conf_d_file_does_not_block_later_conf_d_files(self, config, tmp_path, monkeypatch):
        """
        Regression: config.read() mit der GESAMTEN Dateiliste auf einmal
        bricht beim ersten Parse-Fehler komplett ab - jede danach folgende
        Datei (auch gültige!) wurde dadurch stillschweigend nie gelesen.
        Alphabetisch sortiert landet die kaputte Datei zwischen zwei gültigen.
        """
        conf_path = _write_config(tmp_path, "[settings]\ndefault_category = FromMain\n")
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "10-good.conf").write_text("[settings]\ndefault_tags = good-tag\n", encoding="utf-8")
        (conf_d / "20-broken.conf").write_text("[settings]\ndefault_category\n", encoding="utf-8")
        (conf_d / "30-more.conf").write_text("[settings]\ndefault_language = fr\n", encoding="utf-8")

        monkeypatch.setattr(config, "CONF_PATH", conf_path)
        monkeypatch.setattr(config, "CONF_D_DIR", str(conf_d))
        monkeypatch.delenv("DEFAULT_CATEGORY", raising=False)
        monkeypatch.delenv("DEFAULT_TAGS", raising=False)
        monkeypatch.delenv("VIDEO_LANGUAGE", raising=False)

        config.load_configuration()

        assert config.DEFAULT_CATEGORY == "FromMain"
        assert config.DEFAULT_TAGS == "good-tag"
        assert config.VIDEO_LANGUAGE == "fr"

    def test_unreadable_conf_d_file_is_skipped_with_a_warning(self, config, tmp_path, monkeypatch, caplog):
        """
        Regression: config.read(f, ...) laesst configparser die Datei selbst
        oeffnen - ein dabei auftretender OSError (z.B. Permission denied,
        real reproduziert: eine conf.d-Datei gehoerte einem persoenlichen
        User statt der erwarteten Gruppe) wird von ConfigParser.read()
        INTERN abgefangen und NIE an aufrufenden Code durchgereicht. Die
        Datei wurde dadurch komplett kommentarlos ignoriert, ohne jede
        Log-Warnung. Fix: die Datei selbst oeffnen (config.read_file()),
        damit ein Berechtigungsfehler in unser eigenes except laeuft.
        """
        conf_path = _write_config(tmp_path, "[settings]\ndefault_category = FromMain\n")
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        unreadable = conf_d / "secret.conf"
        unreadable.write_text("[settings]\ndefault_tags = secret-tag\n", encoding="utf-8")

        monkeypatch.setattr(config, "CONF_PATH", conf_path)
        monkeypatch.setattr(config, "CONF_D_DIR", str(conf_d))
        monkeypatch.delenv("DEFAULT_CATEGORY", raising=False)
        monkeypatch.delenv("DEFAULT_TAGS", raising=False)

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path) == str(unreadable):
                raise PermissionError(13, "Permission denied", str(unreadable))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(config, "open", fake_open, raising=False)

        with caplog.at_level("WARNING"):
            config.load_configuration()

        assert config.DEFAULT_CATEGORY == "FromMain"
        assert config.DEFAULT_TAGS != "secret-tag"
        assert "secret.conf" in caplog.text

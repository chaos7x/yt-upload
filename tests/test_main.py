"""
Tests für main.py: CLI-Argument-Parsing (_parse_bool, --embeddable,
-d/--description vs. --description-file) und _apply_description_file().

main() selbst bleibt ungetestet - es orchestriert nur Config-Laden,
Instanz-Lock, Logging-Setup und process_single_file()/find_existing_video()/
run_daemon(), die bereits an anderer Stelle (pipeline.py, daemon.py) getestet
werden. Die tatsächlich neue Logik dieser beiden Features (Bool-Parsing,
Datei-Lesen für die Beschreibung) ist in eigene, isoliert testbare Funktionen
ausgelagert.
"""

import argparse

import pytest


class TestParseBool:
    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on"])
    def test_truthy_values(self, main_module, value):
        assert main_module._parse_bool(value) is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "off"])
    def test_falsy_values(self, main_module, value):
        assert main_module._parse_bool(value) is False

    def test_invalid_value_raises_argument_type_error(self, main_module):
        with pytest.raises(argparse.ArgumentTypeError):
            main_module._parse_bool("maybe")


class TestParseArgumentsRequeueRetries:
    def test_default_is_false(self, main_module):
        args = main_module.parse_arguments(["video.mp4"])
        assert args.requeue_retries is False

    def test_flag_sets_true(self, main_module):
        args = main_module.parse_arguments(["--requeue-retries"])
        assert args.requeue_retries is True


class TestParseArgumentsEmbeddable:
    def test_default_is_none(self, main_module):
        args = main_module.parse_arguments(["video.mp4"])
        assert args.embeddable is None

    def test_explicit_true(self, main_module):
        args = main_module.parse_arguments(["--embeddable", "true", "video.mp4"])
        assert args.embeddable is True

    def test_explicit_false(self, main_module):
        args = main_module.parse_arguments(["--embeddable", "false", "video.mp4"])
        assert args.embeddable is False

    def test_invalid_value_exits(self, main_module):
        with pytest.raises(SystemExit):
            main_module.parse_arguments(["--embeddable", "maybe", "video.mp4"])


class TestParseArgumentsDescriptionFile:
    def test_description_file_alone_is_parsed(self, main_module):
        args = main_module.parse_arguments(["--description-file", "desc.txt", "video.mp4"])
        assert args.description_file == "desc.txt"
        assert args.description is None

    def test_description_alone_is_parsed(self, main_module):
        args = main_module.parse_arguments(["-d", "Hallo Welt", "video.mp4"])
        assert args.description == "Hallo Welt"
        assert args.description_file is None

    def test_description_and_description_file_together_is_rejected(self, main_module):
        with pytest.raises(SystemExit):
            main_module.parse_arguments(["-d", "Hallo", "--description-file", "desc.txt", "video.mp4"])


class TestApplyDescriptionFile:
    def test_no_description_file_is_a_noop(self, main_module):
        args = main_module.parse_arguments(["-d", "Hallo Welt", "video.mp4"])
        main_module._apply_description_file(args)
        assert args.description == "Hallo Welt"

    def test_reads_description_from_file(self, main_module, tmp_path):
        desc_file = tmp_path / "desc.txt"
        desc_file.write_text("Mehrzeilige\nBeschreibung", encoding="utf-8")
        args = main_module.parse_arguments(["--description-file", str(desc_file), "video.mp4"])

        main_module._apply_description_file(args)

        assert args.description == "Mehrzeilige\nBeschreibung"

    def test_missing_description_file_exits(self, main_module, tmp_path):
        missing = tmp_path / "nope.txt"
        args = main_module.parse_arguments(["--description-file", str(missing), "video.mp4"])

        with pytest.raises(SystemExit):
            main_module._apply_description_file(args)


class TestParseArgumentsFiles:
    def test_no_files_is_empty_list(self, main_module):
        args = main_module.parse_arguments(["-a"])
        assert args.files == []

    def test_single_file(self, main_module):
        args = main_module.parse_arguments(["video.mp4"])
        assert args.files == ["video.mp4"]

    def test_multiple_files_preserve_order(self, main_module):
        args = main_module.parse_arguments(["video1.mp4", "video2.mp4", "video3.mp4"])
        assert args.files == ["video1.mp4", "video2.mp4", "video3.mp4"]

    def test_multiple_files_with_flags_mixed_in(self, main_module):
        args = main_module.parse_arguments(["-t", "Konzert", "video1.mp4", "video2.mp4"])
        assert args.files == ["video1.mp4", "video2.mp4"]
        assert args.title == "Konzert"

    def test_default_title_template(self, main_module):
        args = main_module.parse_arguments(["video.mp4"])
        assert args.title_template == "{title} (Teil {n}/{total})"


class TestResolveFileArgs:
    def test_single_file_returns_args_unchanged(self, main_module):
        args = main_module.parse_arguments(["-t", "Konzert", "video.mp4"])

        result = main_module._resolve_file_args(args, index=0, total=1)

        assert result is args
        assert result.title == "Konzert"

    def test_multiple_files_without_explicit_title_leaves_title_none(self, main_module):
        args = main_module.parse_arguments(["video1.mp4", "video2.mp4"])

        result = main_module._resolve_file_args(args, index=0, total=2)

        assert result is args
        assert result.title is None

    def test_multiple_files_with_explicit_title_applies_template(self, main_module):
        args = main_module.parse_arguments(["-t", "Konzert", "video1.mp4", "video2.mp4", "video3.mp4"])

        first = main_module._resolve_file_args(args, index=0, total=3)
        second = main_module._resolve_file_args(args, index=1, total=3)

        assert first.title == "Konzert (Teil 1/3)"
        assert second.title == "Konzert (Teil 2/3)"
        # Das Original bleibt unangetastet, damit jede Iteration von args ausgeht statt kumulativ zu mutieren
        assert args.title == "Konzert"

    def test_custom_title_template_placeholders(self, main_module):
        args = main_module.parse_arguments([
            "-t", "Konzert", "--title-template", "{title} [{n}/{total}]", "video1.mp4", "video2.mp4"
        ])

        result = main_module._resolve_file_args(args, index=1, total=2)

        assert result.title == "Konzert [2/2]"

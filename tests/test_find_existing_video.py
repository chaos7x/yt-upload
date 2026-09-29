"""Tests für daemon.find_existing_video() - welche Container im IN_DIR erkannt werden."""

import pytest


@pytest.mark.parametrize("name", ["a.mp4", "a.mkv", "a.mov", "a.m4v", "a.webm", "A.WEBM"])
def test_detects_supported_containers(daemon, tmp_path, name):
    (tmp_path / name).write_bytes(b"x")
    assert daemon.find_existing_video(str(tmp_path)) == str(tmp_path / name)


@pytest.mark.parametrize("name", ["a.part", "a.jpg", "a.webm.part"])
def test_ignores_other_files(daemon, tmp_path, name):
    (tmp_path / name).write_bytes(b"x")
    assert daemon.find_existing_video(str(tmp_path)) is None

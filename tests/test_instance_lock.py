"""Tests für acquire_instance_lock(): Lock-Datei liegt in WORK_DIR."""

import os

import pytest


@pytest.fixture
def fresh_lock(daemon, monkeypatch):
    if not daemon.HAS_FCNTL:
        pytest.skip("fcntl nicht verfügbar")
    monkeypatch.setattr(daemon, "_lock_file_handle", None)
    yield
    if daemon._lock_file_handle is not None:
        daemon._lock_file_handle.close()


def test_lock_file_is_created_in_work_dir(daemon, config, tmp_path, fresh_lock):
    config.WORK_DIR = str(tmp_path)

    assert daemon.acquire_instance_lock() is True

    lock_path = tmp_path / ".yt-upload.lock"
    assert lock_path.read_text() == str(os.getpid())


def test_second_instance_is_rejected_and_keeps_pid(daemon, config, tmp_path, fresh_lock):
    config.WORK_DIR = str(tmp_path)
    assert daemon.acquire_instance_lock() is True
    first_handle = daemon._lock_file_handle

    # Zweiter Versuch über ein eigenes open() - flock() ist pro offener
    # Dateibeschreibung, verhält sich also wie eine zweite Instanz.
    daemon._lock_file_handle = None
    assert daemon.acquire_instance_lock() is False
    if daemon._lock_file_handle is not None:
        daemon._lock_file_handle.close()
    daemon._lock_file_handle = first_handle

    assert (tmp_path / ".yt-upload.lock").read_text() == str(os.getpid())


def test_missing_work_dir_is_not_reported_as_running_instance(daemon, config, tmp_path, fresh_lock, caplog):
    config.WORK_DIR = str(tmp_path / "gibt-es-nicht")

    assert daemon.acquire_instance_lock() is False
    assert "andere Instanz" not in caplog.text

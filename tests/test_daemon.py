"""
Tests für den Graceful-Shutdown-Mechanismus in daemon.py: SIGTERM (`systemctl
stop`/`docker stop`) und SIGINT sollen dieselbe Wirkung wie ein KeyboardInterrupt
haben (Daemon beendet sich), aber ohne dass eine bereits erkannte Datei
abgebrochen wird - wait_for_input() muss das Flag in beiden Zweigen (Polling-
Fallback und Inotify-Event-Loop) an sinnvollen Stellen prüfen und sauber mit
None zurückkehren, statt eine neue Datei entgegenzunehmen.

Andere daemon.py-Funktionen (find_existing_video, acquire_instance_lock,
run_periodic_tasks) haben bislang keine eigene Testdatei - das bleibt hier
bewusst unangetastet, dieser Test deckt ausschließlich die neue
Signal-Handling-Logik ab.
"""

import signal

import pytest


@pytest.fixture(autouse=True)
def reset_shutdown_flag(daemon):
    """Isoliert das Modul-Flag zwischen Tests, analog zu reset_config_after_test."""
    daemon._shutdown_requested = False
    yield
    daemon._shutdown_requested = False


class TestInstallSignalHandlers:
    def test_registers_handler_for_sigterm_and_sigint(self, daemon, monkeypatch):
        registered = {}

        def fake_signal(sig, handler):
            registered[sig] = handler

        monkeypatch.setattr(daemon.signal, "signal", fake_signal)

        daemon.install_signal_handlers()

        assert registered[signal.SIGTERM] is daemon._handle_shutdown_signal
        assert registered[signal.SIGINT] is daemon._handle_shutdown_signal

    def test_handler_sets_shutdown_flag(self, daemon):
        assert daemon.shutdown_requested() is False

        daemon._handle_shutdown_signal(signal.SIGTERM, None)

        assert daemon.shutdown_requested() is True


class TestWaitForInputPollingFallbackShutdown:
    def test_returns_none_immediately_if_shutdown_already_requested(self, daemon, monkeypatch, tmp_path):
        daemon._shutdown_requested = True

        def forbidden_sleep(_seconds):
            raise AssertionError("time.sleep() sollte bei bereits gesetztem Shutdown-Flag nie aufgerufen werden")

        monkeypatch.setattr(daemon.time, "sleep", forbidden_sleep)
        monkeypatch.setattr(daemon, "HAS_INOTIFY", False)

        result = daemon.wait_for_input(str(tmp_path), inotify_adapter=None)

        assert result is None

    def test_returns_none_after_wakeup_if_shutdown_requested_during_sleep(self, daemon, monkeypatch, tmp_path):
        """Simuliert ein Signal, das waehrend des time.sleep(10) eintrifft."""
        def sleep_then_signal(_seconds):
            daemon._shutdown_requested = True

        monkeypatch.setattr(daemon.time, "sleep", sleep_then_signal)
        monkeypatch.setattr(daemon, "HAS_INOTIFY", False)

        called = {"n": 0}
        monkeypatch.setattr(daemon, "find_existing_video", lambda *_a, **_k: called.update(n=called["n"] + 1) or None)

        result = daemon.wait_for_input(str(tmp_path), inotify_adapter=None)

        assert result is None
        # Nur der EINE unbedingte Check ganz am Anfang von wait_for_input() (vor
        # jeder Schleife) darf gelaufen sein - nach dem waehrend des Sleeps
        # eingetroffenen Signal darf die Schleife find_existing_video() kein
        # weiteres Mal aufrufen.
        assert called["n"] == 1

    def test_finds_existing_file_without_shutdown(self, daemon, monkeypatch, tmp_path):
        """Regressionsschutz: normales Verhalten bleibt unveraendert, wenn nie ein Signal kommt."""
        video = tmp_path / "video.mp4"
        video.write_bytes(b"x")
        monkeypatch.setattr(daemon, "HAS_INOTIFY", False)

        result = daemon.wait_for_input(str(tmp_path), inotify_adapter=None)

        assert result == str(video)


class FakeInotifyAdapter:
    """Double fuer inotify.adapters.InotifyTree - event_gen() liefert vorgegebene Events."""

    def __init__(self, events):
        self._events = list(events)

    def event_gen(self, yield_nones=True, timeout_s=10):
        yield from self._events


class TestWaitForInputInotifyShutdown:
    def test_returns_none_when_shutdown_flagged_between_events(self, daemon, monkeypatch, tmp_path):
        """
        Erstes Event (None, ein Timeout-Tick) wird noch ganz normal verarbeitet;
        dabei setzt find_existing_video() (simuliert) das Shutdown-Flag - das
        zweite Event (eine eigentlich gueltige Datei) darf danach nicht mehr
        ausgewertet werden.
        """
        periodic_calls = {"n": 0}
        find_calls = {"n": 0}

        def fake_periodic():
            periodic_calls["n"] += 1

        def fake_find_existing(*_a, **_k):
            find_calls["n"] += 1
            # Erster Aufruf ist der unbedingte Check ganz am Anfang von
            # wait_for_input() (vor jeder Schleife) - das Signal soll erst
            # danach, waehrend der Loop-internen Suche, eintreffen.
            if find_calls["n"] >= 2:
                daemon._shutdown_requested = True
            return None

        events = [None, (None, ["IN_CLOSE_WRITE"], str(tmp_path), "video.mp4")]
        adapter = FakeInotifyAdapter(events)

        monkeypatch.setattr(daemon, "HAS_INOTIFY", True)
        monkeypatch.setattr(daemon, "run_periodic_tasks", fake_periodic)
        monkeypatch.setattr(daemon, "find_existing_video", fake_find_existing)

        result = daemon.wait_for_input(str(tmp_path), inotify_adapter=adapter)

        assert result is None
        # Nur das erste Event wurde verarbeitet, das zweite (nach dem Signal) nicht mehr
        assert periodic_calls["n"] == 1

    def test_ignores_events_once_shutdown_already_requested(self, daemon, monkeypatch, tmp_path):
        def raise_if_called():
            raise AssertionError("run_periodic_tasks() sollte bei bereits gesetztem Flag nie aufgerufen werden")

        # Ein Event, das eigentlich eine gueltige Datei melden wuerde - darf wegen
        # des vorab gesetzten Flags nie ausgewertet werden.
        events = [(None, ["IN_CLOSE_WRITE"], str(tmp_path), "video.mp4")]
        adapter = FakeInotifyAdapter(events)

        monkeypatch.setattr(daemon, "HAS_INOTIFY", True)
        monkeypatch.setattr(daemon, "run_periodic_tasks", raise_if_called)
        daemon._shutdown_requested = True

        result = daemon.wait_for_input(str(tmp_path), inotify_adapter=adapter)

        assert result is None

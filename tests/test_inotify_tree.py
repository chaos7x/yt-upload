"""
Tests für inotify_tree.InotifyTree (eigener ctypes-Wrapper statt des PyPI-
Pakets "inotify") gegen echte Dateisystem-Events - läuft in CI unter Linux.
"""
import os
import select
import shutil
import threading
import time


def _collect(tree, action, timeout_s=1):
    """Führt action() in einem Thread aus und sammelt alle Nicht-None-Events."""
    threading.Thread(target=action).start()
    return [e for e in tree.event_gen(yield_nones=True, timeout_s=timeout_s) if e is not None]


def _mask(mod):
    return mod.IN_CLOSE_WRITE | mod.IN_MOVED_TO


class TestInotifyTree:
    def test_reports_close_write_with_path_and_filename(self, inotify_tree, tmp_path):
        tree = inotify_tree.InotifyTree(str(tmp_path), mask=_mask(inotify_tree))

        def act():
            time.sleep(0.1)
            (tmp_path / "video.mkv").write_bytes(b"x")

        events = _collect(tree, act)
        assert (None, ["IN_CLOSE_WRITE"], str(tmp_path), "video.mkv") in events

    def test_reports_moved_to(self, inotify_tree, tmp_path):
        src_dir = tmp_path / "outside"
        src_dir.mkdir()
        watched = tmp_path / "watched"
        watched.mkdir()
        (src_dir / "video.mp4").write_bytes(b"x")
        tree = inotify_tree.InotifyTree(str(watched), mask=_mask(inotify_tree))

        def act():
            time.sleep(0.1)
            shutil.move(str(src_dir / "video.mp4"), str(watched / "video.mp4"))

        events = _collect(tree, act)
        assert any("IN_MOVED_TO" in e[1] and e[3] == "video.mp4" for e in events)

    def test_watches_subdirectories_created_after_start(self, inotify_tree, tmp_path):
        tree = inotify_tree.InotifyTree(str(tmp_path), mask=_mask(inotify_tree))

        def act():
            time.sleep(0.1)
            (tmp_path / "sub").mkdir()
            time.sleep(0.3)
            (tmp_path / "sub" / "video.mkv").write_bytes(b"x")

        events = _collect(tree, act)
        assert (None, ["IN_CLOSE_WRITE"], str(tmp_path / "sub"), "video.mkv") in events

    def test_does_not_report_events_outside_mask(self, inotify_tree, tmp_path):
        (tmp_path / "existing.mkv").write_bytes(b"x")
        tree = inotify_tree.InotifyTree(str(tmp_path), mask=_mask(inotify_tree))

        def act():
            time.sleep(0.1)
            os.remove(tmp_path / "existing.mkv")

        assert _collect(tree, act) == []

    def test_event_gen_yields_none_when_idle_and_ends_after_timeout(self, inotify_tree, tmp_path):
        tree = inotify_tree.InotifyTree(str(tmp_path), mask=_mask(inotify_tree))
        start = time.monotonic()
        events = list(tree.event_gen(yield_nones=True, timeout_s=1))
        assert events and all(e is None for e in events)
        assert time.monotonic() - start < 3

    def test_works_without_epoll(self, inotify_tree, tmp_path, monkeypatch):
        # FreeBSD hat inotify (ab 14.5), aber kein epoll - der Wrapper darf
        # epoll daher gar nicht erst benötigen.
        monkeypatch.delattr(select, "epoll", raising=False)
        tree = inotify_tree.InotifyTree(str(tmp_path), mask=_mask(inotify_tree))

        def act():
            time.sleep(0.1)
            (tmp_path / "video.mkv").write_bytes(b"x")

        assert _collect(tree, act)

    def test_close_is_idempotent(self, inotify_tree, tmp_path):
        tree = inotify_tree.InotifyTree(str(tmp_path), mask=_mask(inotify_tree))
        tree.close()
        tree.close()

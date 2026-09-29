"""
Minimaler, rein stdlib-basierter Ersatz für inotify.adapters.InotifyTree
(PyPI "inotify" / Debian python3-inotify, bis 1.12.6 hier genutzt).

Warum: Das Paket wartet per select.epoll() auf Events - epoll gibt es nur
unter Linux. FreeBSD >= 14.5 hat inotify_init1()/inotify_add_watch() nativ
in der libc, aber kein epoll. Dieser Wrapper nutzt dieselben Syscalls per
ctypes und wartet mit select.poll(), das es auf beiden Systemen gibt.

API bewusst kompatibel zum bisher genutzten Teil von InotifyTree:
event_gen(yield_nones=True, timeout_s=N) liefert (None, type_names, path,
filename) bzw. None bei Leerlauf und endet nach timeout_s Sekunden ohne Event.
"""
import ctypes
import ctypes.util
import os
import select
import struct
import time

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_ISDIR = 0x40000000

_NAMES = {
    IN_MODIFY: "IN_MODIFY", IN_CLOSE_WRITE: "IN_CLOSE_WRITE",
    IN_MOVED_FROM: "IN_MOVED_FROM", IN_MOVED_TO: "IN_MOVED_TO",
    IN_CREATE: "IN_CREATE", IN_DELETE: "IN_DELETE",
    IN_DELETE_SELF: "IN_DELETE_SELF", IN_Q_OVERFLOW: "IN_Q_OVERFLOW",
    IN_IGNORED: "IN_IGNORED", IN_ISDIR: "IN_ISDIR",
}

# Intern immer mitabonniert, damit neu angelegte/hineinverschobene
# Unterverzeichnisse automatisch mitüberwacht werden (wie InotifyTree).
_TREE_MASK = IN_CREATE | IN_MOVED_TO | IN_DELETE_SELF

_EVENT_HDR = struct.Struct("iIII")  # wd, mask, cookie, len - gleiches Layout Linux/FreeBSD

# find_library("c") liefert unter musl (Alpine) oft None - CDLL(None) lädt
# dann die Symbole des laufenden Python-Prozesses, zu denen die libc gehört.
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
try:
    _libc.inotify_init1.argtypes = [ctypes.c_int]
    _libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
except AttributeError as e:
    # z.B. OpenBSD, macOS oder FreeBSD < 14.5: libc ohne inotify-Syscalls.
    raise ImportError(f"libc stellt kein inotify bereit: {e}") from e


def _check(ret):
    if ret == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return ret


class InotifyTree:
    def __init__(self, path, mask):
        self._mask = mask
        self._fd = None
        self._fd = _check(_libc.inotify_init1(os.O_CLOEXEC))
        self._wd_to_path = {}
        self._poll = select.poll()
        self._poll.register(self._fd, select.POLLIN)
        self._add_tree(os.fsdecode(path))

    def _add_watch(self, path):
        try:
            wd = _check(_libc.inotify_add_watch(
                self._fd, os.fsencode(path), self._mask | _TREE_MASK | IN_ONLYDIR))
        except OSError:
            return  # Verzeichnis inzwischen weg / keine Rechte - wie InotifyTree überspringen
        self._wd_to_path[wd] = path

    def _add_tree(self, root):
        self._add_watch(root)
        for dirpath, dirnames, _ in os.walk(root):
            for d in dirnames:
                self._add_watch(os.path.join(dirpath, d))

    def _read_events(self):
        buf = os.read(self._fd, 64 * 1024)
        offset = 0
        while offset < len(buf):
            wd, mask, _cookie, length = _EVENT_HDR.unpack_from(buf, offset)
            offset += _EVENT_HDR.size
            name = buf[offset:offset + length].rstrip(b"\0").decode("utf-8", "surrogateescape")
            offset += length
            yield wd, mask, name

    def event_gen(self, yield_nones=True, timeout_s=None):
        last_event = time.monotonic()
        while True:
            if timeout_s is not None and time.monotonic() - last_event >= timeout_s:
                return
            if not self._poll.poll(1000):
                if yield_nones:
                    yield None
                continue
            for wd, mask, name in self._read_events():
                path = self._wd_to_path.get(wd)
                if mask & IN_IGNORED:
                    self._wd_to_path.pop(wd, None)
                    continue
                if path is None:
                    continue
                if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                    self._add_tree(os.path.join(path, name))
                if not mask & self._mask:
                    continue
                last_event = time.monotonic()
                type_names = [n for bit, n in _NAMES.items() if mask & bit]
                yield (None, type_names, path, name)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    # Wie beim bisherigen InotifyTree: FD beim Verwerfen des Objekts schließen
    # (yt-upload legt bei geänderter IN_DIR einfach eine neue Instanz an).
    __del__ = close

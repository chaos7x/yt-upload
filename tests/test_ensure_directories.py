"""
Tests für ensure_directories() (fileutils.py).

Regression: os.makedirs(d, exist_ok=True) ohne explizites mode= verliert
das Gruppen-Schreibrecht durchs Prozess-Umask (Standard-Mode 0o777 wird
umask-maskiert, z.B. auf 0o755 bei umask 022) - das Setgid-Bit selbst wird
zwar vom Elternverzeichnis /srv/media-pipeline geerbt, das für die
media-pipeline-Gruppe eigentlich nötige g+w aber nicht. Derselbe Bug wurde
real auf einem tw-recorder-Host gefunden (Kanal-Unterordner standen auf
2755 statt 2775) - ohne den Fix könnten tw-recorder/fetchbridge
(Gruppenmitglieder, aber nicht Owner) hier keine Dateien mehr
ablegen/verschieben.
"""

import os
import stat


class TestEnsureDirectories:
    def test_newly_created_dirs_get_2775_regardless_of_umask(self, fileutils, config, monkeypatch, tmp_path):
        old_umask = os.umask(0o022)
        try:
            in_dir = str(tmp_path / "incoming")
            work_dir = str(tmp_path / "work")
            done_dir = str(tmp_path / "done")
            corrupt_dir = str(tmp_path / "corrupt")
            retry_dir = str(tmp_path / "retry")
            monkeypatch.setattr(config, "IN_DIR", in_dir)
            monkeypatch.setattr(config, "WORK_DIR", work_dir)
            monkeypatch.setattr(config, "DONE_DIR", done_dir)
            monkeypatch.setattr(config, "CORRUPT_DIR", corrupt_dir)
            monkeypatch.setattr(config, "RETRY_DIR", retry_dir)

            fileutils.ensure_directories()

            for d in (in_dir, work_dir, done_dir, corrupt_dir, retry_dir):
                assert stat.S_IMODE(os.stat(d).st_mode) == 0o2775
        finally:
            os.umask(old_umask)

    def test_2775_without_setgid_chmod_under_restrict_suid_sgid(self, fileutils, config, monkeypatch, tmp_path):
        """
        Regression: systemd RestrictSUIDSGID=true lehnt chmod() mit Setgid-Bit
        per seccomp ab (EPERM) - die Verzeichnisse müssen trotzdem 2775
        bekommen (Setgid geerbt vom Elternverzeichnis, g+w über makedirs()).
        """
        parent = tmp_path / "media-pipeline"
        parent.mkdir()
        parent.chmod(0o2775)
        dirs = [str(parent / n) for n in ("incoming", "work", "done", "corrupt", "retry")]
        for name, d in zip(("IN_DIR", "WORK_DIR", "DONE_DIR", "CORRUPT_DIR", "RETRY_DIR"), dirs, strict=True):
            monkeypatch.setattr(config, name, d)
        real_chmod = fileutils.os.chmod

        def restricted_chmod(path, mode, *args, **kwargs):
            if mode & (stat.S_ISUID | stat.S_ISGID):
                raise PermissionError("Operation not permitted")
            return real_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(fileutils.os, "chmod", restricted_chmod)
        old_umask = os.umask(0o022)
        try:
            fileutils.ensure_directories()
            for d in dirs:
                assert stat.S_IMODE(os.stat(d).st_mode) == 0o2775
            assert os.umask(0o022) == 0o022  # Prozess-Umask wiederhergestellt
        finally:
            os.umask(old_umask)

    def test_existing_dir_permissions_are_not_overwritten(self, fileutils, config, monkeypatch, tmp_path):
        """Eine bewusste Admin-Anpassung (z.B. chmod 777) darf nicht überschrieben werden."""
        in_dir = tmp_path / "incoming"
        in_dir.mkdir()
        in_dir.chmod(0o777)
        monkeypatch.setattr(config, "IN_DIR", str(in_dir))
        monkeypatch.setattr(config, "WORK_DIR", str(tmp_path / "work"))
        monkeypatch.setattr(config, "DONE_DIR", str(tmp_path / "done"))
        monkeypatch.setattr(config, "CORRUPT_DIR", str(tmp_path / "corrupt"))
        monkeypatch.setattr(config, "RETRY_DIR", str(tmp_path / "retry"))

        fileutils.ensure_directories()

        assert stat.S_IMODE(in_dir.stat().st_mode) == 0o777

    def test_chmod_failure_does_not_raise(self, fileutils, config, monkeypatch, tmp_path, capsys):
        in_dir = str(tmp_path / "incoming")
        monkeypatch.setattr(config, "IN_DIR", in_dir)
        monkeypatch.setattr(config, "WORK_DIR", str(tmp_path / "work"))
        monkeypatch.setattr(config, "DONE_DIR", str(tmp_path / "done"))
        monkeypatch.setattr(config, "CORRUPT_DIR", str(tmp_path / "corrupt"))
        monkeypatch.setattr(config, "RETRY_DIR", str(tmp_path / "retry"))

        def raise_oserror(path, mode):
            raise OSError("Operation not permitted")

        monkeypatch.setattr(fileutils.os, "chmod", raise_oserror)

        fileutils.ensure_directories()  # darf nicht raisen

        assert os.path.isdir(in_dir)

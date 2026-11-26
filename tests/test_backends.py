import os
from pathlib import Path
import tempfile
import unittest

from naui.backends import (ConflictError, DisconnectedError, FileError, LocalFileBackend,
                           RemoteFileBackend)


class LocalTransport:
    """Execute the EXACT remote scripts locally without a test SSH server.

    Exercises quoting, framing, conflicts and replacement. This does not claim
    authentication, network or real remote-host acceptance.
    """
    host = "test-local-script"

    def command(self, script, *, batch=False):
        return ["bash", "-c", script]


class BackendChecks:
    def test_create_save_read_permissions(self):
        path = str(Path(self.root, "test.txt"))
        snapshot = self.backend.read(path)
        self.assertEqual(snapshot.revision, "missing")
        written = self.backend.write(path, "Hello from Naui\nõ", snapshot.revision)
        self.assertEqual(self.backend.read(path).content, "Hello from Naui\nõ")
        self.assertNotEqual(written.revision, "missing")
        os.chmod(path, 0o640)
        before = self.backend.read(path)
        self.backend.write(path, "changed", before.revision)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertEqual(list(Path(self.root).glob(".naui-*")), [])

    def test_conflict_preserves_destination(self):
        path = Path(self.root, "file")
        path.write_text("original")
        snapshot = self.backend.read(str(path))
        path.write_text("external change")
        with self.assertRaises(ConflictError):
            self.backend.write(str(path), "my unsaved edits", snapshot.revision)
        self.assertEqual(path.read_text(), "external change")

    def test_new_file_conflict(self):
        path = Path(self.root, "new")
        path.write_text("already created")
        with self.assertRaises(ConflictError):
            self.backend.write(str(path), "overwrite", "missing")
        self.assertEqual(path.read_text(), "already created")

    def test_metacharacter_paths_never_executed(self):
        name = "space '\";$(touch NAUI_INJECTION)\\\nõ.txt"
        path = str(Path(self.root, name))
        written = self.backend.write(path, "text\r\n", "missing")
        self.assertEqual(written.content, "text\r\n")
        self.assertEqual(self.backend.read(path).content, "text\r\n")
        self.assertFalse(Path("NAUI_INJECTION").exists())

    def test_symlink_preserved(self):
        target = Path(self.root, "target")
        target.write_text("old")
        link = Path(self.root, "link")
        link.symlink_to("target")
        snapshot = self.backend.read(str(link))
        self.backend.write(str(link), "new", snapshot.revision)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "new")

    def test_trailing_newline_in_filename_and_directory(self):
        directory = Path(self.root, "directory\n")
        directory.mkdir()
        path = str(directory / "filename\n")
        snapshot = self.backend.write(path, "newline paths", "missing")
        self.assertEqual(snapshot.path, path)
        self.assertEqual(self.backend.read(path).content, "newline paths")

    def test_reject_binary_non_utf8_and_directory(self):
        for name, data in [("binary", b"a\0b"), ("bad-utf8", b"\xff")]:
            path = Path(self.root, name)
            path.write_bytes(data)
            with self.assertRaises(FileError):
                self.backend.read(str(path))
        with self.assertRaises((FileError, IsADirectoryError)):
            self.backend.read(self.root)


class LocalBackendTests(BackendChecks, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name
        self.backend = LocalFileBackend()


class RemoteScriptTests(BackendChecks, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name
        self.backend = RemoteFileBackend(LocalTransport())

    def test_disconnected_preserves_file(self):
        path = Path(self.root, "file")
        path.write_text("old")
        snapshot = self.backend.read(str(path))
        self.backend.connected = False
        with self.assertRaises(DisconnectedError):
            self.backend.write(str(path), "new", snapshot.revision)
        self.assertEqual(path.read_text(), "old")

    def test_incomplete_transfer_is_not_installed(self):
        from naui.backends import REMOTE_WRITE
        path = Path(self.root, "file")
        path.write_text("old")
        snapshot = self.backend.read(str(path))
        with self.assertRaises(FileError):
            self.backend._run(REMOTE_WRITE, [str(path), snapshot.revision, "100"], b"partial")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(Path(self.root).glob(".naui-*")), [])

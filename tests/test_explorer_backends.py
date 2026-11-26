import os
from pathlib import Path
import tempfile
import unittest

from naui.backends import ConflictError, LocalFileBackend, RemoteFileBackend
from test_backends import LocalTransport


class ExplorerBackendChecks:
    def test_structured_listing_unicode_and_symlinks(self):
        directory = Path(self.root, "a dir ' õ\n")
        directory.mkdir()
        file = Path(self.root, "file;$(touch NAUI_BAD)'\nõ")
        file.write_text("hello")
        Path(self.root, ".hidden").write_text("")
        Path(self.root, "linked-dir").symlink_to(directory.name)
        Path(self.root, "broken-link").symlink_to("missing")
        before = os.getcwd()
        entries = self.backend.list_directory(self.root)
        self.assertEqual(os.getcwd(), before)
        records = {entry.name: entry for entry in entries}
        self.assertEqual(set(records), {directory.name, file.name, ".hidden", "linked-dir", "broken-link"})
        self.assertTrue(records[directory.name].is_directory)
        self.assertTrue(records["linked-dir"].is_directory)
        self.assertTrue(records["linked-dir"].is_symlink)
        self.assertTrue(records["broken-link"].is_symlink)
        self.assertEqual(records[file.name].size, 5)
        self.assertEqual(records[file.name].path, str(file))
        self.assertTrue(entries[0].is_directory)
        self.assertFalse(Path("NAUI_BAD").exists())

    def test_create_rename_delete_and_no_overwrite(self):
        path = str(Path(self.root, "a; 'õ\n"))
        self.backend.create(path)
        self.assertEqual(Path(path).read_bytes(), b"")
        with self.assertRaises(OSError):
            self.backend.create(path)
        entry = self.backend.properties(path)
        existing = str(Path(self.root, "existing"))
        Path(existing).write_text("preserve")
        with self.assertRaises(OSError):
            self.backend.rename(path, existing, entry.revision)
        self.assertTrue(Path(path).exists())
        self.assertEqual(Path(existing).read_text(), "preserve")
        renamed = str(Path(self.root, "new 'name\n"))
        self.backend.rename(path, renamed, entry.revision)
        self.assertFalse(Path(path).exists())
        updated = self.backend.properties(renamed)
        self.backend.delete(renamed, updated.revision)
        self.assertFalse(Path(renamed).exists())

    def test_directory_create_delete_refuses_nonempty(self):
        directory = str(Path(self.root, "new-dir"))
        self.backend.create(directory, directory=True)
        Path(directory, "child").write_text("preserve")
        entry = self.backend.properties(directory)
        with self.assertRaises(OSError):
            self.backend.delete(directory, entry.revision)
        self.assertEqual(Path(directory, "child").read_text(), "preserve")
        Path(directory, "child").unlink()
        entry = self.backend.properties(directory)
        self.backend.delete(directory, entry.revision)
        self.assertFalse(Path(directory).exists())

    def test_delete_symlink_only_and_selection_conflict(self):
        target = Path(self.root, "target")
        target.write_text("preserve")
        link = Path(self.root, "link")
        link.symlink_to(target.name)
        entry = self.backend.properties(str(link))
        self.backend.delete(str(link), entry.revision)
        self.assertTrue(target.exists())
        entry = self.backend.properties(str(target))
        target.write_text("changed")
        with self.assertRaises(ConflictError):
            self.backend.delete(str(target), entry.revision)
        self.assertEqual(target.read_text(), "changed")

    def test_root_properties(self):
        self.assertTrue(self.backend.properties("/").is_directory)


class LocalExplorerTests(ExplorerBackendChecks, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.backend = LocalFileBackend()


class RemoteExplorerScriptTests(ExplorerBackendChecks, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.backend = RemoteFileBackend(LocalTransport())

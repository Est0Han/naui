import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from naui.rpf import ArchiveReader, child_path, hex_preview
from naui.viewer_registry import ViewerRegistry


class RpfTests(unittest.TestCase):
    def test_signature_routes_binary_even_with_text_extension(self):
        for magic in (b'RPF7', b'8FPR', b'9FPR'):
            self.assertEqual(ViewerRegistry().select('wrong.txt', magic + b'\0' * 20).kind, 'rpf')
        self.assertEqual(ViewerRegistry().select('note.rpf', b'plain text').kind, 'text')

    def test_reject_archive_path_traversal(self):
        for name in ('../escape', '/absolute', '.', '..', 'a/b', 'a\\b', '\0', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                child_path('archive.rpf', name)
        self.assertEqual(child_path('archive.rpf', 'a file.xml'), 'archive.rpf/a file.xml')

    def test_copy_does_not_replace_prior_edit_or_archive(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_DATA_HOME': directory}):
            archive = Path(directory) / 'test.rpf'
            archive.write_bytes(b'9FPR original')
            reader = ArchiveReader(archive)
            with patch.object(reader, 'run', return_value=b'<root/>'):
                first = Path(reader.extract('test.rpf/file', xml=True))
                first.write_text('<edited/>')
                second = Path(reader.extract('test.rpf/file', xml=True))
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), '<edited/>')
            self.assertEqual(second.read_text(), '<root/>')
            self.assertEqual(archive.read_bytes(), b'9FPR original')

    def test_bounded_hex_and_cancelled_job(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large.rpf'
            with path.open('wb') as file:
                file.write(b'9FPR'); file.truncate(1 << 30)
            self.assertEqual(len(hex_preview(path).splitlines()), 256)
            reader = ArchiveReader(path)
            reader.close()
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                reader.listing(path.name)

    def test_cli_arguments_are_not_shell_interpreted(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'helper'
            helper.write_text('#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            helper.chmod(0o700)
            reader = ArchiveReader(Path(directory) / 'a.rpf')
            with patch('naui.rpf.helper_path', return_value=str(helper)):
                import json
                result = json.loads(reader.run('ls', '--', 'a.rpf/$(touch bad)'))
            self.assertEqual(result[-1], 'a.rpf/$(touch bad)')
            self.assertFalse((Path(directory) / 'bad').exists())

    def test_readable_metadata_chooses_xml(self):
        reader = ArchiveReader('/tmp/test.rpf')
        with patch.object(reader, 'extract', return_value='/tmp/metadata.xml') as extract:
            self.assertEqual(reader.readable('test.rpf/entry.#mt'), '/tmp/metadata.xml')
            extract.assert_called_once_with('test.rpf/entry.#mt', xml=True)

    def test_readable_text_and_binary_are_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / 'entry'
            reader = ArchiveReader('/tmp/test.rpf')
            with patch.object(reader, 'extract', return_value=str(raw)):
                raw.write_text('Readable source\n')
                self.assertEqual(reader.readable('test.rpf/entry'), str(raw))
                # A text prefix must not conceal binary bytes later in the file.
                raw.write_bytes(b'A' * 8192 + b'\0Binary string')
                report = Path(reader.readable('test.rpf/entry.#script'))
                self.assertNotEqual(raw, report)
                self.assertIn('NOT SOURCE CODE', report.read_text())
                self.assertIn('decoding is not supported', report.read_text())
                self.assertIn('Binary string', report.read_text())
                self.assertEqual(raw.read_bytes(), b'A' * 8192 + b'\0Binary string')

    def test_readable_script_disassembly_is_labeled(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / 'test.ysc'; raw.write_bytes(b'\0script')
            reader = ArchiveReader('/tmp/test.rpf')
            with patch.object(reader, 'extract', return_value=str(raw)), patch.object(reader, 'run', return_value=b'ENTER 0') as run:
                result = Path(reader.readable('test.rpf/test.ysc'))
                run.assert_called_once_with('script', '--', 'test.rpf/test.ysc')
                self.assertTrue(result.name.endswith('.disassembly.txt'))
                self.assertEqual(result.read_bytes(), b'ENTER 0')

import codecs
from pathlib import Path
import tempfile
import unittest

from naui.binary import inspect_binary, SCAN_LIMIT, RAW_LIMIT
from naui.viewer_registry import ViewerRegistry


class BinaryTests(unittest.TestCase):
    def inspect(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.bin'
            path.write_bytes(data)
            result = inspect_binary(path)
            self.assertEqual(path.read_bytes(), data)
            return result

    def test_strings_raw_and_nontext(self):
        reports = self.inspect(b'\x00\xff\x01hello world\x00' + 'wide text'.encode('utf-16le'))
        self.assertIn('hello world', reports['Strings'])
        self.assertIn('wide text', reports['Strings'])
        self.assertIn('00 ff 01', reports['Raw bytes'])
        self.assertIn('not plain', reports['Text'])

    def test_empty_unicode_and_utf16(self):
        self.assertIn('0 bytes', self.inspect(b'')['Overview'])
        self.assertIn('café', self.inspect('café'.encode())['Text'])
        self.assertIn('hello', self.inspect(codecs.BOM_UTF16_LE + 'hello'.encode('utf-16le'))['Text'])

    def test_large_file_is_bounded(self):
        data = b'\x00' * (SCAN_LIMIT + 1) + b'beyond scan'
        reports = self.inspect(data)
        self.assertNotIn('beyond scan', reports['Strings'])
        self.assertIn(f'First {RAW_LIMIT:,}', reports['Raw bytes'])
        self.assertLess(len(reports['Raw bytes']), 400000)

    def test_routing_retains_known_formats_and_text(self):
        registry = ViewerRegistry()
        self.assertEqual(registry.select('data.BIN', b'\x00\xff', 'application/octet-stream').kind, 'binary')
        self.assertEqual(registry.select('data.bin', b'plain text').kind, 'text')
        self.assertEqual(registry.select('data.bin', b'\x89PNG\r\n\x1a\n').kind, 'image')
        self.assertEqual(registry.select('data.bin', b'MZ').kind, 'executable')
        self.assertEqual(registry.select('data.bin', b'RPF7').kind, 'rpf')
        self.assertEqual(registry.select('data.unknown', b'\x00\xff').kind, 'unsupported')

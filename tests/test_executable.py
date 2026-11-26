import struct
import tempfile
from pathlib import Path
import unittest
from naui.executable import inspect_executable
from naui.viewer_registry import ViewerRegistry


def fixture(bits=64, managed=False):
    data = bytearray(1024)
    data[:2] = b'MZ'; struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    optional_size = 240 if bits == 64 else 224
    struct.pack_into('<HHIIIHH', data, 132, 0x8664 if bits == 64 else 0x14c, 1, 0, 0, 0, optional_size, 2)
    start = 152
    struct.pack_into('<H', data, start, 0x20b if bits == 64 else 0x10b)
    struct.pack_into('<I', data, start+16, 0x1000)
    struct.pack_into('<Q' if bits == 64 else '<I', data, start+(24 if bits == 64 else 28), 0x400000)
    directory = start+(112 if bits == 64 else 96)
    struct.pack_into('<I', data, directory-4, 16)
    if managed: struct.pack_into('<II', data, directory+14*8, 0x2000, 72)
    section = start+optional_size
    data[section:section+8] = b'.text\0\0\0'
    struct.pack_into('<IIII', data, section+8, 100, 0x1000, 100, 512)
    struct.pack_into('<I', data, section+36, 0x60000000)
    data[512:525] = b'Hello binary!'
    data[600:624] = 'Hello wide!!'.encode('utf-16le')
    return bytes(data)


class ExecutableTests(unittest.TestCase):
    def inspect(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'test.exe'; path.write_bytes(data)
            result = inspect_executable(path)
            self.assertEqual(path.read_bytes(), data)
            return result

    def test_32_and_64_bit_headers(self):
        for bits in (32,64):
            report = self.inspect(fixture(bits))
            self.assertIn(f'({bits}-bit)', report['Overview'])
            self.assertIn('.text', report['Sections'])
            self.assertIn('RX', report['Sections'])
            self.assertIn('Hello binary!', report['Strings'])
            self.assertIn('Hello wide!!', report['Strings'])

    def test_clr_and_invalid_headers(self):
        self.assertIn('present (.NET', self.inspect(fixture(managed=True))['Overview'])
        for data in (b'',b'MZ',b'not an executable',b'MZ'+b'\xff'*200):
            self.assertIn('Header inspection:',self.inspect(data)['Overview'])
        data=bytearray(fixture()); struct.pack_into('<H',data,134,65535)
        self.assertIn('Unsupported section count',self.inspect(data)['Overview'])

    def test_detection(self):
        registry=ViewerRegistry()
        self.assertEqual(registry.select('wrong.txt',fixture()).kind,'executable')
        self.assertEqual(registry.select('test.dll',fixture()).kind,'executable')
        self.assertEqual(registry.select('test.exe',b'plain text').kind,'text')

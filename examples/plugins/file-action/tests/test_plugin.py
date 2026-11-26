import unittest
from types import SimpleNamespace
from plugin import Plugin
from naui_plugin_api import FileLocation


class FileTests(unittest.TestCase):
    def test_remote_identity_is_preserved(self):
        registered, reads, messages = [], [], []
        def read(location, callback):
            reads.append(location); callback('abc', None)
        api = SimpleNamespace(files=SimpleNamespace(add_action=lambda *args: registered.append(args), read=read),
                              notifications=SimpleNamespace(show=messages.append))
        Plugin().activate(api)
        location = FileLocation('remote-test', '/path/file.txt')
        registered[0][3](location, SimpleNamespace(kind='f'))
        self.assertEqual(reads, [location])
        self.assertEqual(messages, ['remote-test: 3 characters'])

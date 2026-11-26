import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from naui.chat_attachments import Attachments

class AttachmentTests(TestCase):
    def test_bounds_private_copy_and_cleanup(self):
        store=Attachments()
        try:
            item=store.add_bytes('../hello.txt', b'hello\0world')
            path=Path(item['path'])
            self.assertEqual(path.read_bytes(), b'hello\0world')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(item['name'], 'hello.txt')
            with patch('naui.chat_attachments.MAX_TOTAL', 12):
                with self.assertRaises(ValueError): store.add_bytes('more', b'ab')
            with patch('naui.chat_attachments.MAX_FILE', 2):
                with self.assertRaises(ValueError): store.add_bytes('big', b'abc')
            with tempfile.TemporaryDirectory() as folder:
                fifo=Path(folder)/'pipe';os.mkfifo(fifo)
                with self.assertRaises(ValueError): store.add_file(fifo)
        finally: store.close()
        self.assertFalse(path.exists())

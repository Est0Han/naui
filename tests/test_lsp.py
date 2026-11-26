import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest

from naui.backends import LocalFileBackend
from naui.code_navigation import file_uri
from naui.lsp import LspClient, LspError
from naui.lsp_workspace import LspSession
from naui.workspace import RemoteDeviceBackend
from test_workspace import RootedTransport
from lsp_helpers import connect_virtual_machine

FAKE_COMMAND = [sys.executable, str(Path(__file__).with_name('fake_lsp.py'))]


class StdioClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = LspClient(FAKE_COMMAND, self.directory.name)
        self.addCleanup(self.client.close)
        self.client.start(file_uri(self.directory.name))

    def test_initialize_full_sync_and_graceful_idempotent_shutdown(self):
        uri = file_uri(str(Path(self.directory.name, 'õ.py')))
        self.client.notify('textDocument/didOpen', {'textDocument': {'uri': uri, 'languageId': 'python', 'version': 1, 'text': 'x = "😀"'}})
        self.client.notify('textDocument/didChange', {'textDocument': {'uri': uri, 'version': 2}, 'contentChanges': [{'text': 'new = "õ"'}]})
        self.assertEqual(self.client.request('naui/capture', {})[uri]['text'], 'new = "õ"')
        self.client.close(); self.client.close()
        self.assertEqual(self.client.process.returncode, 0)
        self.assertTrue(all(not thread.is_alive() for thread in self.client._threads))

    def test_request_timeout_cancellation_and_close_pending_request(self):
        with self.assertRaisesRegex(LspError, 'timed out'):
            self.client.request('naui/hang', {}, timeout=0.05)
        errors = []
        def pending():
            try:
                self.client.request('naui/hang', {}, timeout=5)
            except LspError as error:
                errors.append(error)
        thread = threading.Thread(target=pending)
        thread.start()
        time.sleep(0.05)
        self.client.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.client._pending)

    def test_bad_server_frame_fails_pending_request_without_hanging_editor(self):
        with self.assertRaisesRegex(LspError, 'Content-Length'):
            self.client.request('naui/bad-frame', {})
        self.assertTrue(any(event['method'] == 'naui/error' for event in self.client.drain()))

    def test_late_closed_document_work_cannot_replace_a_reopened_document(self):
        session = LspSession('local', LocalFileBackend(), self.directory.name, command=FAKE_COMMAND)
        self.addCleanup(session.close)
        path = str(Path(self.directory.name, 'main.py'))
        session.sync(path, 'old = 1', 1)
        session.detach(path, 1)
        session.sync(path, 'new = 2', 3)
        session.detach(path, 1)  # Delayed close from the original editor tab.
        with self.assertRaises(LspError):
            session.sync(path, 'old unsaved = 3', 1)
        captures = session.client.request('naui/capture', {})
        self.assertEqual(captures[session.mapper.uri(path)]['text'], 'new = 2')
        self.assertEqual(captures[session.mapper.uri(path)]['version'], 3)

    def test_reindex_restarts_failed_server_and_reaps_its_old_process(self):
        session = LspSession('local', LocalFileBackend(), self.directory.name, command=FAKE_COMMAND)
        self.addCleanup(session.close)
        path = str(Path(self.directory.name, 'main.py'))
        session.sync(path, 'value = 1', 1)
        old_client = session.client
        with self.assertRaises(LspError):
            old_client.request('naui/bad-frame', {})
        session.refresh()
        session.sync(path, 'value = 2', 2)
        self.assertIsNot(session.client, old_client)
        self.assertIsNotNone(old_client.process.returncode)
        self.assertEqual(session.client.request('naui/capture', {})[session.mapper.uri(path)]['text'], 'value = 2')


@unittest.skipUnless(os.environ.get('NAUI_TEST_PYRIGHT_COMMAND'), 'Set NAUI_TEST_PYRIGHT_COMMAND to opt into real Pyright integration')
class PyrightIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.command = shlex.split(os.environ['NAUI_TEST_PYRIGHT_COMMAND'])

    def test_real_definition_references_hover_diagnostics_and_buffer_rename(self):
        content = 'def greet(name: str) -> str:\n    return name\n\ngreet("hello")\nnumber: int = "wrong"\n'
        path = str(self.root / 'main.py')
        Path(path).write_text(content)
        session = LspSession('local', LocalFileBackend(), str(self.root), command=self.command)
        self.addCleanup(session.close)
        position = {'line': 3, 'character': 2}
        definition = session.query('definition', path, content, 1, position)
        self.assertEqual(session.mapper.location(definition[0]['uri']).path, path)
        references = session.query('references', path, content, 1, position, context={'includeDeclaration': True})
        self.assertEqual(len(references), 2)
        hover = session.query('hover', path, content, 1, position)
        self.assertIn('greet', str(hover['contents']))
        rename = session.query('rename', path, content, 1, position, newName='welcome')
        plan = session.rename_plan(rename, {path: (1, content)})
        self.assertEqual(plan[0].content.count('welcome'), 2)
        self.assertEqual(Path(path).read_text(), content)  # Planning never writes.
        diagnostics = []
        deadline = time.monotonic() + 10
        while not diagnostics and time.monotonic() < deadline:
            diagnostics += [event for event in session.client.drain() if event.get('method') == 'textDocument/publishDiagnostics'
                            and event.get('params', {}).get('diagnostics')]
            time.sleep(0.02)
        self.assertTrue(diagnostics)
        self.assertEqual(diagnostics[0]['params']['version'], 1)
        self.assertTrue(any(item['code'] == 'reportAssignmentType' for item in diagnostics[0]['params']['diagnostics']))
        corrected = content.replace('"wrong"', '42')
        session.query('hover', path, corrected, 2, position)
        self.assertEqual(session.documents[path], (2, corrected))
        session.cancel()
        self.assertEqual(session.client.process.returncode, 0)

    def test_real_remote_import_navigation_and_multi_file_rename_keep_remote_identity(self):
        machine = self.root / 'machine'
        project = machine / 'project'
        project.mkdir(parents=True)
        content = 'from helper import greet\ngreet("hello")\n'
        (project / 'main.py').write_text(content)
        (project / 'helper.py').write_text('def greet(name: str) -> str:\n    return name\n')
        backend = RemoteDeviceBackend('server-a')
        connect_virtual_machine(backend, 'server-a', machine)
        self.addCleanup(backend.close)
        session = LspSession('ssh-a', backend, '/project', remote=True, command=self.command, cache_base=self.root / 'cache')
        self.addCleanup(session.close)
        position = {'line': 1, 'character': 2}
        definition = session.query('definition', '/project/main.py', content, 1, position)
        target = session.mapper.location(definition[0]['uri'], definition[0]['range']['start'])
        self.assertEqual(target.device_id, 'ssh-a')
        self.assertEqual(target.path, '/project/helper.py')
        rename = session.query('rename', '/project/main.py', content, 1, position, newName='welcome')
        plan = session.rename_plan(rename, {'/project/main.py': (1, content)})
        self.assertEqual({item.location.path for item in plan}, {'/project/helper.py', '/project/main.py'})
        self.assertTrue(all(item.location.device_id == 'ssh-a' for item in plan))
        self.assertEqual((project / 'main.py').read_text(), content)
        (project / 'helper.py').write_text('def greet(changed: int) -> int:\n    return changed\n')
        with self.assertRaisesRegex(LspError, 'changed since analysis'):
            session.rename_plan(rename, {'/project/main.py': (1, content)})
        mirror = Path(session.mirror.directory.name)
        session.cancel(); session.close()
        self.assertFalse(mirror.exists())
        self.assertEqual(session.client.process.returncode, 0)

    def test_real_unsaved_unicode_document_and_repeated_clean_server_shutdown(self):
        path = str(self.root / 'main.py')
        text = 'def õ():\n    return 1\n\nvalue = "😀"; õ()\n'
        for _ in range(3):
            session = LspSession('local', LocalFileBackend(), str(self.root), command=self.command)
            try:
                # The file does not exist: Pyright must use didOpen's buffer.
                definition = session.query('definition', path, text, 1, {'line': 3, 'character': 15})
                self.assertTrue(definition)
            finally:
                session.cancel(); session.close()
            self.assertEqual(session.client.process.returncode, 0)
            self.assertTrue(all(not thread.is_alive() for thread in session.client._threads))

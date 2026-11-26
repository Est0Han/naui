import io
import os
import re
from pathlib import Path
import tempfile
import unittest

from naui.code_navigation import (CodeHistory, CodeLocation, TERMINAL_PATH_PATTERN, TextPositions, UriMapper,
                                  apply_text_edits, edit_groups, file_uri, offset_at,
                                  path_links, position_at, uri_path)
from naui.lsp import LspClient, LspError, encode_message, read_message
from naui.lsp_workspace import SourceMirror, project_root
from naui.workspace import RemoteDeviceBackend
from test_workspace import RootedTransport


def text_edit(start, end, text, line=0):
    return {"range": {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}}, "newText": text}


class PositionsAndNavigationTests(unittest.TestCase):
    def test_utf16_emoji_crlf_and_unicode_separators(self):
        text = 'a😀b\r\nx\u2028y\n'
        self.assertEqual(position_at(text, 2), {"line": 0, "character": 3})
        self.assertEqual(offset_at(text, {"line": 0, "character": 3}), 2)
        self.assertEqual(offset_at(text, {"line": 1, "character": 2}), 7)
        self.assertEqual(offset_at(text, {"line": 2, "character": 0}), len(text))
        with self.assertRaises(ValueError):
            offset_at(text, {"line": 0, "character": 2})
        self.assertEqual(offset_at(text, {"line": 999, "character": 0}, clamp=True), len(text))
        with self.assertRaises(ValueError):
            offset_at(text, {"line": True, "character": 0})

    def test_edits_are_validated_and_applied_from_original_utf16_positions(self):
        text = '😀foo + foo'
        self.assertEqual(apply_text_edits(text, [text_edit(2, 5, 'long_name'), text_edit(8, 11, 'long_name')]), '😀long_name + long_name')
        for edits in ([text_edit(1, 2, 'bad')], [text_edit(2, 8, 'a'), text_edit(5, 9, 'b')],
                      [text_edit(9, 5, 'bad')], [text_edit(100, 101, 'bad')], [{"range": [], "newText": "bad"}]):
            with self.assertRaises(ValueError):
                apply_text_edits(text, edits)

    def test_navigation_history_retains_device_identity_and_branches(self):
        history = CodeHistory()
        a, b, c = (CodeLocation(device, '/project/main.py', 2, 3) for device in ('local', 'ssh-a', 'ssh-b'))
        history.add(a); history.add(b); history.add(c)
        self.assertEqual(history.target(-1), b)
        history.accept(-1)
        self.assertEqual(history.target(-1), a)
        history.add(a)
        self.assertIsNone(history.target(1))
        self.assertEqual(history.entries, [a, b, a])

    def test_path_links_spaces_unicode_tracebacks_and_literal_shell_text(self):
        text = 'File "/tmp/õ x.py", line 12\n"./$(touch nope).py":4:2\n~/src/main.py:3:4\nfile:///etc/a%20b.conf:5\n'
        links = path_links(text, '/project', 'ssh-a', '/home/remote', remote=True)
        self.assertEqual([link.location.path for link in links], ['/tmp/õ x.py', '/project/$(touch nope).py', '/home/remote/src/main.py', '/etc/a b.conf'])
        self.assertEqual(links[0].location.line, 11)
        self.assertEqual(links[1].location.character, 1)
        self.assertTrue(all(link.location.device_id == 'ssh-a' for link in links))

    def test_external_urls_are_not_file_paths_or_device_selectors(self):
        self.assertEqual(path_links('https://example.com/foo.py ssh://other/project/x.py "https://host/a.py"', '/tmp', 'local'), [])
        with self.assertRaises(ValueError):
            uri_path('file://other-machine/etc/passwd')
        with self.assertRaises(ValueError):
            uri_path('file:///tmp/a%00.py')
        matched = re.search(TERMINAL_PATH_PATTERN, 'https://example.com/foo.py').group()
        self.assertEqual(matched, 'https://example.com/foo.py')
        self.assertEqual(path_links(matched, '/project', 'local'), [])

    def test_large_unbroken_source_line_does_not_trigger_quadratic_link_scanning(self):
        self.assertEqual(path_links('a' * 1000000, '/project', 'local'), [])

    def test_mime_independent_file_uri_roundtrip(self):
        path = '/tmp/õ % # quote\' space.py'
        self.assertEqual(uri_path(file_uri(path)), path)
        a, b = UriMapper('local', '/tmp'), UriMapper('ssh-a', '/tmp')
        self.assertNotEqual(a.location(file_uri(path)).device_id, b.location(file_uri(path)).device_id)


class MapperAndMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_remote_mapper_rejects_other_devices_local_files_and_symlink_escape(self):
        a, b = self.root / 'a', self.root / 'b'
        a.mkdir(); b.mkdir()
        mapper = UriMapper('ssh-a', '/project', str(a))
        uri = mapper.uri('/project/õ main.py')
        self.assertEqual(mapper.location(uri), CodeLocation('ssh-a', '/project/õ main.py'))
        for foreign in (file_uri(str(b / 'main.py')), file_uri('/usr/lib/stub.pyi'), file_uri(str(a / '../escape.py'))):
            with self.assertRaises(ValueError):
                mapper.location(foreign)
        (a / 'link').symlink_to(b, target_is_directory=True)
        with self.assertRaises(ValueError):
            mapper.uri('/project/link/main.py')

    def test_workspace_rename_rejects_resource_ops_duplicate_uris_and_outside_root(self):
        mapper = UriMapper('local', str(self.root))
        uri = mapper.uri(str(self.root / 'main.py'))
        edits = [text_edit(0, 1, 'x')]
        self.assertEqual(edit_groups({'changes': {uri: edits}}, mapper)[0][0].device_id, 'local')
        for edit in ({'changes': {file_uri('/etc/passwd'): edits}},
                     {'documentChanges': [{'kind': 'rename', 'oldUri': uri, 'newUri': uri}]},
                     {'documentChanges': [{'textDocument': {'uri': uri}, 'edits': edits}] * 2},
                     {'changes': {uri: edits}, 'documentChanges': []}):
            with self.assertRaises(ValueError):
                edit_groups(edit, mapper)

    def test_local_marker_discovery_does_not_inspect_the_local_disk_for_remote_roots(self):
        (self.root / 'project/src').mkdir(parents=True)
        (self.root / 'project/pyproject.toml').write_text('')
        path = str(self.root / 'project/src/main.py')
        self.assertEqual(project_root(path, [str(self.root)]), str(self.root / 'project'))
        self.assertEqual(project_root(path, [str(self.root)], remote=True), str(self.root))

    def test_remote_mirror_is_bounded_private_ignores_configs_symlinks_and_cleans_up(self):
        machine = self.root / 'machine'
        project = machine / 'project'
        (project / '.venv').mkdir(parents=True)
        (project / 'a.py').write_text('value = 1\n')
        (project / 'b.py').write_text('value = 2\n')
        (project / 'pyrightconfig.json').write_text('{"pythonPath": "untrusted-command"}')
        (project / '.venv/hidden.py').write_text('hidden = True')
        (project / 'alias.py').symlink_to(project / 'a.py')
        backend = RemoteDeviceBackend('server')
        backend.connect(RootedTransport('server', machine))
        self.addCleanup(backend.close)
        mirror = SourceMirror(backend, '/project', self.root / 'cache', files=1)
        self.addCleanup(mirror.close)
        mirror.prepare()
        directory = Path(mirror.directory.name)
        self.assertEqual(list(directory.rglob('*.py')), [directory / 'a.py'])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'a.py').stat().st_mode & 0o777, 0o600)
        self.assertFalse((directory / 'pyrightconfig.json').exists())
        self.assertIn('partial', mirror.warning)
        mirror.close(); mirror.close()
        self.assertFalse(directory.exists())


class FramingTests(unittest.TestCase):
    def test_unicode_frames_fragmented_body_and_multiple_messages(self):
        class Fragmented(io.BytesIO):
            def read(self, size=-1): return super().read(min(size, 2))
        message = {'jsonrpc': '2.0', 'id': 1, 'result': '😀 õ'}
        stream = Fragmented(encode_message(message) * 2)
        self.assertEqual(read_message(stream), message)
        self.assertEqual(read_message(stream), message)
        self.assertIsNone(read_message(stream))

    def test_invalid_oversize_truncated_and_non_utf8_frames(self):
        for data in (b'Content-Length: 99999999999\r\n\r\n', b'Content-Length: 10\r\nContent-Length: 10\r\n\r\n',
                     b'Content-Length: 10\n\n', b'Content-Length: 8\r\n\r\n{}',
                     b'Content-Length: 2\r\nContent-Type: application/vscode-jsonrpc; charset=latin1\r\n\r\n{}',
                     b'Content-Length: 2\r\n\r\n\xff\xff', encode_message({'jsonrpc': '2.0', 'method': []})):
            with self.assertRaises(LspError):
                read_message(io.BytesIO(data))

    def test_server_requests_cannot_apply_edits_or_execute_commands(self):
        client = LspClient(['unused'], '/tmp', {'python': {'analysis': {'diagnosticMode': 'openFilesOnly'}}})
        sent = []
        client._send = sent.append
        client._server_request({'id': 1, 'method': 'workspace/applyEdit', 'params': {'edit': {'changes': {}}}})
        self.assertFalse(sent[-1]['result']['applied'])
        client._server_request({'id': 2, 'method': 'workspace/executeCommand', 'params': {'command': 'untrusted'}})
        self.assertEqual(sent[-1]['error']['code'], -32601)
        client._server_request({'id': 3, 'method': 'workspace/configuration', 'params': {'items': [{'section': 'python.analysis'}]}})
        self.assertEqual(sent[-1]['result'][0]['diagnosticMode'], 'openFilesOnly')

    def test_notification_queue_is_bounded_and_oversize_diagnostics_reported(self):
        client = LspClient(['unused'], '/tmp')
        for number in range(300):
            client._enqueue({'method': 'example', 'params': {'number': number}})
        self.assertEqual(len(client.drain()), 256)
        client._enqueue({'method': 'textDocument/publishDiagnostics', 'params': {'message': 'x' * (5 * 1024 * 1024)}})
        self.assertEqual(client.drain()[0]['method'], 'naui/error')
        self.assertEqual(client._message_bytes, 0)

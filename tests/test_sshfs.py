"""SSHFS ownership/mapping tests with mocked mounts; no external SSH host."""
from dataclasses import replace
from pathlib import Path
import os
import tempfile
import json
import sys
import shlex
import subprocess
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from naui.backends import LocalFileBackend, RemoteFileBackend, ConflictError
from naui.code_navigation import UriMapper, file_uri
from naui.preview import PreviewCache
from naui.ssh import Connection
from naui.sshfs import (SSHFSFileBackend, SSHFSSupport, Mount, detect_support,
                        device_connection, mount_command, logical_path)
from naui.workspace import RemoteDeviceBackend, Workspace, WorkspaceStore
from test_backends import LocalTransport
from naui.cache_sessions import mark_cache, cleanup_stale_caches, MARKER
from naui.plugins.base_lsp.workspace import LspSession, SourceMirror

SUPPORT = SSHFSSupport('/mock/sshfs', '/mock/fusermount3', True, '', 'test')


class SSHFSTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.mounts = SSHFSFileBackend('ssh-test', Connection('user@example-server'),
                                     support=SUPPORT, base=self.root/'runtime/mounts')
        self.mounts.set_roots(['/project'])
        self.physical = self.root/'mounted'
        self.physical.mkdir()
        self.process = Mock()
        self.process.poll.return_value = None
        self.mounts._mounts['/project'] = Mount('/project', self.physical, self.process)

    def test_detection_missing_tool_and_fuse_are_nonfatal(self):
        with patch('naui.sshfs.shutil.which', return_value=None):
            self.assertFalse(detect_support().usable)
        with patch('naui.sshfs.shutil.which', side_effect=lambda name: '/mock/'+name), patch('naui.sshfs.os.access', return_value=False):
            self.assertIn('FUSE', detect_support().reason)

    def test_mapping_roundtrip_preserves_spaces_unicode_and_device(self):
        path = '/project/õ space ;$(data).py'
        mapped = self.mounts.local_path(path)
        self.assertEqual(mapped, str(self.physical/'õ space ;$(data).py'))
        self.assertEqual(self.mounts.device_path(mapped), path)
        mapper = UriMapper('ssh-test', '/project', str(self.physical))
        self.assertEqual(mapper.location(mapper.uri(path)).device_id, 'ssh-test')
        self.assertEqual(mapper.location(file_uri(mapped)).path, path)
        with self.assertRaises(ValueError):
            self.mounts.device_path(str(self.root/'other'))

    def test_no_implicit_root_mount_or_traversal_or_local_symlink_escape(self):
        self.assertIsNone(self.mounts.local_path('/etc/config'))
        for path in ('relative', '/project/../outside', '/project/\0'):
            with self.assertRaises(ValueError): logical_path(path)
        (self.physical/'escape').symlink_to(self.root)
        with self.assertRaises(OSError): self.mounts.local_path('/project/escape/secret')
        for identity in ('../escape', 'bad/name', '', 'bad\0'):
            with self.assertRaises(ValueError): SSHFSFileBackend(identity, Connection('server'))

    def test_unpin_only_changes_metadata_then_worker_unmounts(self):
        with patch('naui.sshfs.subprocess.run') as command:
            self.mounts.set_roots([])
            command.assert_not_called()
            self.assertTrue(self.physical.exists())
        with patch('naui.sshfs.stop_process'):
            self.mounts.cleanup_removed()
        self.assertFalse(self.physical.exists())

    def test_command_uses_independent_openssh_and_readonly_sftp_data(self):
        connection = Connection('user@server', ('-p', '2222', '-S', '/private/tab.sock',
                                                '-o', 'ControlMaster=auto', '-J', 'example-jump'))
        argv = mount_command(SUPPORT, connection, '/project ;$(not-executed)', self.physical)
        self.assertIn('user@server:/project ;$(not-executed)', argv)
        self.assertIn('ro', argv)
        self.assertNotIn('/private/tab.sock', ' '.join(argv))
        self.assertIn('example-jump', ' '.join(argv))
        self.assertNotIn('-S', device_connection(connection).options)
        with self.assertRaises(ValueError):
            mount_command(SUPPORT, Connection('server', ('-i', 'key,ambiguous')), '/project', self.physical)

    def test_mount_lazy_one_root_and_failure_does_not_retry_loop(self):
        other = SSHFSFileBackend('ssh-new', Connection('server'), support=SUPPORT, base=self.root/'runtime/mounts')
        self.addCleanup(other.close)
        with patch('naui.sshfs.subprocess.Popen', side_effect=OSError('private internal detail')) as spawn:
            other.set_roots(['/project', '/other'])
            spawn.assert_not_called()
            self.assertIsNone(other.local_path('/project/file'))
            self.assertIsNone(other.local_path('/project/file2'))
            self.assertEqual(spawn.call_count, 1)
            self.assertNotIn(str(self.root), other.warning)
            self.assertNotIn('private internal detail', other.warning)

    def test_mount_success_uses_private_runtime_and_does_not_scan_project(self):
        other = SSHFSFileBackend('ssh-new', Connection('server'), support=SUPPORT, base=self.root/'runtime/mounts')
        other.set_roots(['/project'])
        with patch('naui.sshfs.subprocess.Popen', return_value=self.process), patch('naui.sshfs.os.path.ismount', return_value=True):
            path = other.local_path('/project/file')
            self.assertTrue(path.startswith(str(self.root/'runtime/mounts')))
            self.assertEqual(other._directory.stat().st_mode & 0o777, 0o700)
        with patch('naui.sshfs.os.path.ismount', return_value=False), patch('naui.sshfs.stop_process'):
            other.close()
            other.close()
        self.assertFalse(other._mounts)

    def test_stale_owned_mount_directory_is_detected_but_never_deleted(self):
        base = self.root/'runtime/mounts'
        stale = base/'ssh-old-abcdefgh'
        stale.mkdir(mode=0o700, parents=True)
        base.chmod(0o700)
        base.parent.chmod(0o700)
        stale.joinpath('owner.json').write_text(json.dumps({'format':1,'uid':os.getuid(),'pid':1}))
        other = SSHFSFileBackend('ssh-new', Connection('server'), support=SUPPORT, base=base)
        with patch('naui.sshfs.os.kill', side_effect=ProcessLookupError):
            other._runtime()
        self.assertIn('older Naui', other.notice)
        other.close()
        self.assertTrue(stale.is_dir())

    def test_marked_dead_caches_cleaned_live_unknown_and_symlinks_preserved(self):
        base = self.root/'cache'
        base.mkdir()
        stale, live, unknown = [base/name for name in ('preview-old','preview-live','preview-unknown')]
        for directory in (stale,live,unknown): directory.mkdir(mode=0o700)
        mark_cache(stale, 'preview'); mark_cache(live, 'preview')
        data = json.loads((stale/MARKER).read_text()); data['pid'] = 1
        (stale/MARKER).write_text(json.dumps(data))
        (base/'preview-link').symlink_to(unknown)
        def process(pid, signal):
            if pid == 1: raise ProcessLookupError()
        with patch('naui.cache_sessions.os.kill', side_effect=process): cleanup_stale_caches(base, 'preview')
        self.assertFalse(stale.exists())
        self.assertTrue(live.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue((base/'preview-link').is_symlink())

    def test_direct_lsp_uses_mount_uri_and_never_writes_saved_buffer_to_mount(self):
        file = self.physical/'main.py'
        file.write_text('source = 1\n')
        command = [sys.executable,str(Path(__file__).with_name('fake_lsp.py'))]
        session = LspSession('ssh-test', self.mounts, '/project', remote=True, command=command, cache_base=self.root/'cache')
        self.addCleanup(session.close)
        session.sync('/project/main.py','unsaved = 2\n',1)
        self.assertIsNone(session.mirror)
        self.assertEqual(session.client.cwd, session._python_runtime.name)
        self.assertEqual(session.client.settings['python']['pythonPath'], session._isolated_python())
        self.assertEqual(session.client.root_uri, file_uri(str(self.physical)))
        uri = session.mapper.uri('/project/main.py')
        self.assertEqual(session.mapper.location(uri).device_id, 'ssh-test')
        session.saved('/project/main.py','buffer = 3\n',2)
        self.assertEqual(file.read_text(),'source = 1\n')
        settings = session.client.settings['python']['analysis']
        self.assertEqual(settings['diagnosticMode'],'openFilesOnly')
        self.assertFalse(settings['indexing'])
        self.assertNotIn('**/.venv',settings['exclude'])
        session.client._enqueue({'method':'textDocument/publishDiagnostics','params':{'uri':uri,
                                'diagnostics':[{'message':'Problem in '+str(file)}]}})
        event = session.client.drain()[-1]
        self.assertEqual(event['params']['uri'], uri)
        self.assertNotIn(str(self.physical),event['params']['diagnostics'][0]['message'])
        self.assertIn('/project/main.py',event['params']['diagnostics'][0]['message'])

    def test_rename_null_result_can_recover_without_applying_any_edits(self):
        session = LspSession('local',LocalFileBackend(),str(self.physical))
        self.addCleanup(session.close)
        session.mapper = UriMapper('local',str(self.physical))
        session.client = Mock(capabilities={'renameProvider':True})
        edits = {'changes':{file_uri(str(self.physical/'main.py')):[]}}
        session.client.request.side_effect = [None, edits]
        result = session.query('rename',str(self.physical/'main.py'),'name=1',1,{'line':0,'character':0},newName='other')
        self.assertEqual(result, edits)
        self.assertEqual(session.client.request.call_count,2)
        self.assertFalse((self.physical/'main.py').exists())

    def test_source_mirror_quota_applies_across_refresh_and_editor_saves(self):
        for index in range(4): (self.physical/f'part{index}.py').write_text('x'*10)
        mirror = SourceMirror(LocalFileBackend(), str(self.physical), self.root/'cache',files=20,byte_limit=30)
        self.addCleanup(mirror.close)
        mirror.prepare()
        self.assertFalse(mirror.write_source(str(self.physical/'part0.py'), b'a'*20))
        self.assertTrue(mirror.write_source(str(self.physical/'part0.py'), b'a'*5))
        self.assertTrue(mirror.write_source(str(self.physical/'new.py'), b'b'*5))
        mirror.prepare()  # Partial refresh must not reset byte accounting.
        self.assertFalse(mirror.write_source(str(self.physical/'four.py'), b'd'*20))
        self.assertLessEqual(sum(path.stat().st_size for path in Path(mirror.directory.name).rglob('*.py')), 30)

    @unittest.skipUnless(os.environ.get('NAUI_TEST_PYRIGHT_COMMAND'), 'Optional real Pyright command not configured')
    def test_real_pyright_definitions_translate_mounted_project_to_remote_device(self):
        canary = self.root/'executed-remote-code'
        (self.physical/'sitecustomize.py').write_text(
            'from pathlib import Path\nPath(' + repr(str(canary)) + ').touch()\n')
        content = 'from helper import greet\ngreet("hello")\n'
        (self.physical/'main.py').write_text(content)
        (self.physical/'helper.py').write_text('def greet(name: str) -> str:\n    return name\n')
        session = LspSession('ssh-test', self.mounts, '/project', remote=True,
                             command=shlex.split(os.environ['NAUI_TEST_PYRIGHT_COMMAND']))
        self.addCleanup(session.close)
        definitions = session.query('definition', '/project/main.py', content, 1, {'line':1,'character':2})
        location = session.mapper.location(definitions[0]['uri'])
        self.assertEqual((location.device_id,location.path), ('ssh-test','/project/helper.py'))
        self.assertIsNone(session.mirror)
        self.assertFalse(canary.exists(), 'Opening remote source must not execute its sitecustomize')

    def test_remote_interpreter_probe_is_isolated_and_cleanup_is_idempotent(self):
        canary = self.root/'executed-remote-code'
        (self.physical/'sitecustomize.py').write_text(
            'from pathlib import Path\nPath(' + repr(str(canary)) + ').touch()\n')
        session = LspSession('ssh-test',self.mounts,'/project',remote=True)
        self.addCleanup(session.close)
        wrapper = session._isolated_python()
        result = subprocess.run([wrapper,'-c','import json, sys; print(json.dumps(sys.path))'],
                                cwd=self.physical, capture_output=True, text=True, check=True)
        self.assertNotIn(str(self.physical), json.loads(result.stdout))
        self.assertFalse(canary.exists())
        session.close()
        session.close()
        self.assertFalse(Path(wrapper).exists())

    def test_unmount_idempotent_and_busy_mount_not_deleted(self):
        with patch('naui.sshfs.os.path.ismount', return_value=True), patch('naui.sshfs.subprocess.run', return_value=Mock(returncode=1)):
            self.assertFalse(self.mounts.unmount('/project'))
            self.assertTrue(self.physical.is_dir())
            self.assertIn('/project', self.mounts._mounts)
        with patch('naui.sshfs.os.path.ismount', return_value=False), patch('naui.sshfs.stop_process') as stop:
            self.assertTrue(self.mounts.unmount('/project'))
            self.assertTrue(self.mounts.unmount('/project'))
            stop.assert_called_once()

    def test_stale_external_directory_never_removed_or_unmounted(self):
        stale = self.root/'runtime/mounts/unknown-owner'
        stale.mkdir(parents=True)
        with patch('naui.sshfs.subprocess.run') as command:
            self.mounts.close()
            self.assertTrue(stale.is_dir())
            command.assert_not_called()

    def test_direct_media_never_downloaded_deleted_or_leaks_logical_path(self):
        media = self.physical/'movie.mp4'
        media.write_bytes(b'\x00media')
        self.assertEqual(self.mounts.probe('/project/movie.mp4').path, '/project/movie.mp4')
        cache = PreviewCache(self.root/'cache')
        self.addCleanup(cache.close)
        source = cache.prepare(self.mounts, '/project/movie.mp4', remote=True)
        self.assertEqual(source.local_path, str(media))
        self.assertFalse(source.cached)
        cache.release(source)
        self.assertTrue(media.exists())
        self.assertIsNone(cache._directory)

    def test_aggregate_cache_cap_and_release(self):
        path = self.root/'media'
        path.write_bytes(b'a'*8)
        cache = PreviewCache(self.root/'cache', limit=10, cache_limit=15)
        self.addCleanup(cache.close)
        a = cache.prepare(LocalFileBackend(), str(path), remote=True)
        with self.assertRaises(OSError): cache.prepare(LocalFileBackend(), str(path), remote=True)
        cache.release(a)
        b = cache.prepare(LocalFileBackend(), str(path), remote=True)
        self.assertTrue(b.cached)

    def test_device_mount_survives_one_and_final_terminal_release(self):
        backend = RemoteDeviceBackend('user@example-server')
        backend.device_id = 'ssh-test'
        backend.connect(Connection('user@example-server'), 't1')
        backend.connect(Connection('user@example-server'), 't2')
        backend._sshfs = self.mounts
        with patch('naui.sshfs.stop_process'):
            self.assertTrue(backend.release('t1'))
            self.assertTrue(backend.has_session('t2'))
            self.assertTrue(self.mounts.mounted)
            self.assertTrue(backend.release('t2'))
            self.assertTrue(backend.connected)
            self.assertTrue(self.mounts.mounted)
            owned = backend._device_delegate
            backend.connect(Connection('user@example-server'), 't3')
            backend.select('t3')
            backend.close()
            backend.close()
            backend.cleanup_mounts()
            self.assertFalse(backend.connected)
            self.assertFalse(self.mounts.mounted)
            self.assertFalse(owned.connected)

    def test_unavailable_automatic_and_explicit_modes_keep_existing_backend(self):
        file = self.root/'file'
        file.write_text('original')
        backend = RemoteDeviceBackend('server')
        backend.device_id = 'ssh-test'
        backend.connect(Connection('server'), 't1')
        backend._delegate.close()
        backend._delegate = RemoteFileBackend(LocalTransport())
        backend._sessions['t1'] = backend._delegate
        self.addCleanup(backend.close)
        backend.set_roots([str(self.root)])
        with patch('naui.sshfs.detect_support', return_value=replace(SUPPORT, usable=False, reason='SSHFS unavailable')):
            for mode in ('automatic', 'sshfs', 'compatibility'):
                backend.set_filesystem_mode(mode)
                self.assertIn('file', [entry.name for entry in backend.list_directory(str(self.root))])
                original = backend.read(str(file))
                backend.write(str(file), 'saved', original.revision)
                self.assertEqual(file.read_text(), 'saved')
                file.write_text('original')
                self.assertIsNone(backend.local_path(str(file)))
        self.assertIn('compatibility', backend.filesystem_status)

    def test_remote_commands_transfers_and_watch_stay_ssh(self):
        backend = RemoteDeviceBackend('server')
        backend.connect(Connection('server'), 't1')
        real = backend._delegate
        backend._delegate = Mock(connected=True)
        self.addCleanup(real.close)
        backend._sshfs = self.mounts
        for method, args in [('run_command', (['git','status'], '/project')),
                             ('watch_snapshot', ('/project',)), ('transfer_properties', ('/project/file',))]:
            getattr(backend, method)(*args)
            getattr(backend._delegate, method).assert_called_once_with(*args)

    def test_filesystem_without_remote_terminal_cannot_navigate_local_shell(self):
        from naui.plugins.base_file_explorer.explorer import ExplorerSidebar
        window = SimpleNamespace(activate_device=Mock(), navigate_terminal=Mock())
        sidebar = SimpleNamespace(_shutting_down=False, device=SimpleNamespace(id='ssh-test',type='ssh'),
                                  session=None, window=window, status=Mock())
        self.assertFalse(ExplorerSidebar.navigate(sidebar,'/project'))
        window.activate_device.assert_called_once_with('ssh-test')
        window.navigate_terminal.assert_not_called()

    def test_activating_mounted_device_without_shell_starts_its_own_terminal(self):
        from naui.gui import NauiWindow
        device = SimpleNamespace(id='ssh-test', type='ssh', state='connected')
        window = SimpleNamespace(_shutting_down=False,workspace=SimpleNamespace(devices={'ssh-test':device},active_device_id='local'),
                                  device_terminal=lambda identity:None,connect_device=Mock())
        self.assertTrue(NauiWindow.activate_device(window,'ssh-test'))
        window.connect_device.assert_called_once_with(device,new_terminal=True)

    def test_mode_and_roots_persist_but_mount_paths_do_not(self):
        workspace = Workspace(str(self.root))
        device = workspace.add_ssh('user@example-server')
        device.add_root('/project')
        device.filesystem_backend.filesystem_mode = 'compatibility'
        store = WorkspaceStore(self.root/'workspace.json')
        store.save(workspace)
        restored = Workspace(str(self.root), store.load()).devices[device.id]
        self.assertEqual(restored.filesystem_backend.filesystem_mode, 'compatibility')
        self.assertEqual(restored.filesystem_backend._roots, ('/project',))
        self.assertNotIn('mounts', store.path.read_text())
        self.assertFalse(restored.connected)
        records = workspace.records()
        records[1]['filesystem_mode'] = 'unsupported'
        with self.assertRaises(ValueError): WorkspaceStore.validate({'version':1,'devices':records})


if __name__ == '__main__':
    unittest.main()

"""Public capability checks, metadata, explicit user loading and plugin shutdown."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from naui.plugin_manager import PluginManager, PluginSpec
from naui.transfers import FileLocation
from naui.workspace import Workspace


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.host = SimpleNamespace(_shutting_down=False, workspace=Workspace(str(self.root)),
            set_status=self.calls.append, work=lambda job, done: done(job(), None))
        self.host.plugins = self.manager = PluginManager(self.host)
        self.addCleanup(self.manager.close)
        self.host.transfers = SimpleNamespace(copy=lambda source, destination: 'owned-job',
            cancel=lambda identity: self.calls.append(identity), status=lambda identity: {'id': identity})
        self.host.workspace_sidebar = SimpleNamespace(active_explorer=SimpleNamespace(device=self.host.workspace.devices['local'],
            selected_path=lambda: str(self.root / 'selected')))

    def activate(self, permissions, start=lambda api: None, identity='test-plugin'):
        return self.manager.activate(PluginSpec(identity, 'Example', frozenset(permissions)), start, permissions)

    def test_ungranted_permissions_reject_before_plugin_entrypoint(self):
        spec = PluginSpec('test-plugin', 'Example', frozenset({'file_transfer'}))
        with self.assertRaises(PermissionError):
            self.manager.activate(spec, lambda api: self.calls.append('executed'), set())
        self.assertEqual(self.calls, [])

    def test_remote_requires_separate_permission_and_no_raw_auth_api(self):
        remote = self.host.workspace.add_ssh('user@host')
        api = self.activate({'filesystem_read'})
        self.assertFalse(hasattr(api, 'authentication'))
        self.assertFalse(hasattr(api, 'ssh'))
        self.assertEqual([device.type for device in api.devices()], ['local'])
        with self.assertRaises(PermissionError):
            api.properties(FileLocation(remote.id, '/file'), lambda *_: None)
        with self.assertRaises(PermissionError):
            api.register_feature('raw-window', object())

    def test_transfers_require_read_write_transfer_and_remote_grants_and_scope_job_ids(self):
        a = self.host.workspace.add_ssh('a'); b = self.host.workspace.add_ssh('b')
        for index, permissions in enumerate(({'file_transfer'}, {'filesystem_read', 'filesystem_write', 'file_transfer'})):
            api = self.activate(permissions, identity='test-' + str(index))
            with self.assertRaises(PermissionError):
                api.transfers.copy(FileLocation(a.id, '/source'), FileLocation(b.id, '/target'))
        api = self.activate({'filesystem_read', 'filesystem_write', 'file_transfer', 'remote_access'})
        job = api.transfers.copy(FileLocation(a.id, '/source'), FileLocation(b.id, '/target'))
        self.assertEqual(api.transfers.status(job), {'id': 'owned-job'})
        api.transfers.cancel(job)
        self.assertEqual(self.calls, ['owned-job'])
        with self.assertRaises(PermissionError):
            api.transfers.status('someone-elses-job')

    def test_safe_read_create_and_selected_location(self):
        path = self.root / 'selected'; path.write_text('hello')
        api = self.activate({'filesystem_read', 'filesystem_write'})
        self.assertEqual(api.selected_location(), FileLocation('local', str(path)))
        results = []
        api.read_text(api.selected_location(), lambda content, error: results.append((content, error)))
        self.assertEqual(results, [('hello', None)])
        api.create(FileLocation('local', str(self.root / 'new')), callback=lambda error: self.assertIsNone(error))
        self.assertTrue((self.root / 'new').exists())

    def test_selected_directory_preserves_remote_identity_and_permissions(self):
        remote = self.host.workspace.add_ssh('user@host')
        self.host.workspace_sidebar.active_explorer = SimpleNamespace(
            device=remote, selected_parent=lambda: '/srv/project')
        denied = self.activate({'filesystem_read'}, identity='local-only')
        with self.assertRaises(PermissionError):
            denied.workspace.selected_directory()
        api = self.activate({'filesystem_read', 'remote_access'})
        self.assertEqual(api.workspace.selected_directory(), FileLocation(remote.id, '/srv/project'))

    def test_local_command_workspace_reports_disconnected_and_can_reconnect(self):
        from unittest.mock import Mock, patch
        remote = self.host.workspace.add_ssh('user@host')
        self.host.activate_device = Mock(return_value=True)
        api = self.activate({'remote_access', 'command_execution', 'terminal_execution'})
        with self.assertRaisesRegex(ValueError, '^SSH_WORKSPACE_DISCONNECTED$'):
            api.devices.command_workspace(device_id=remote.id, cwd='/srv/project')
        api.devices.connect(remote.id)
        self.host.activate_device.assert_called_once_with(remote.id, reconnect=True)
        remote.state = 'connected'
        with patch.dict('os.environ', {'XDG_CACHE_HOME': str(self.root/'cache')}):
            result = api.devices.command_workspace(device_id=remote.id, cwd='/srv/project')
        self.assertEqual(result['device_id'], remote.id)
        self.assertEqual(result['remote_cwd'], '/srv/project')
        self.assertTrue(Path(result['cwd']).is_dir())
        with patch('naui.ssh_workspace.prepare_workspace', side_effect=OSError('private details')):
            with self.assertRaisesRegex(ValueError, '^SSH_WORKSPACE_LOCAL_STORAGE$'):
                api.devices.command_workspace(device_id=remote.id, cwd='/srv/project')

    def test_plugin_cannot_reconnect_without_terminal_grant(self):
        remote = self.host.workspace.add_ssh('user@host')
        api = self.activate({'remote_access'})
        with self.assertRaises(PermissionError): api.devices.connect(remote.id)

    def test_commands_are_removed_and_window_released_on_idempotent_shutdown(self):
        stopped = []
        def start(api):
            api.add_command('command', 'Example command', lambda: api.message('called'))
            return SimpleNamespace(stop=lambda: stopped.append(True))
        api = self.activate({'commands'}, start)
        self.manager.invoke('test-plugin', 'command')
        self.assertEqual(self.calls, ['called'])
        self.manager.close(); self.manager.close()
        self.assertEqual(stopped, [True])
        self.assertFalse(self.manager.commands)
        self.assertIsNone(api._host)
        self.assertIsNone(self.manager.host)
        with self.assertRaises(RuntimeError):
            api.message('late')

    def test_user_manifest_grants_checked_before_module_level_code(self):
        manifest = {'id': 'test-user', 'name': 'Example', 'api_version': 1, 'permissions': ['commands']}
        (self.root / 'plugin.json').write_text(json.dumps(manifest))
        (self.root / 'plugin.py').write_text("raise RuntimeError('module executed')\ndef start(api): pass\n")
        with self.assertRaises(PermissionError):
            self.manager.load_user(self.root, set())
        with self.assertRaisesRegex(RuntimeError, 'module executed'):
            self.manager.load_user(self.root, {'commands'})
        manifest['id'] = 'base-filepath'
        (self.root / 'plugin.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.manager.inspect_user(self.root)

    def test_example_loads_only_through_public_api_and_is_user_installed(self):
        example = Path(__file__).resolve().parents[1] / 'examples/plugins/inspect-selection'
        (self.root / 'selected').write_text('hello')
        self.manager.load_user(example, {'commands', 'filesystem_read', 'remote_access'})
        self.manager.invoke('inspect-selection', 'inspect')
        self.assertIn('5 bytes', self.calls[-1])
        self.assertFalse(self.manager.plugins['inspect-selection'][0].bundled)

    def test_bundled_registration_includes_language_provider_and_core_services_remain_owned(self):
        try:
            import gi
            for name, version in [('Gtk', '4.0'), ('Vte', '3.91'), ('GtkSource', '5')]:
                gi.require_version(name, version)
            self.manager.load_bundled()
        except (ImportError, ValueError):
            self.skipTest('GTK bindings unavailable for bundled frontend adapters')
        self.assertEqual(set(self.manager.plugins), {'base-file-explorer', 'base-filepath', 'base-lsp', 'python-lsp', 'base-media', 'base-runner', 'base-git'})
        self.assertTrue(all(spec.bundled and spec.enabled_by_default for spec, _ in self.manager.plugins.values()))
        self.assertIn('.py', self.manager.features['languages'])
        self.assertIn('explorer_transfers', self.manager.features)
        self.assertNotIn('authentication', self.manager.features)

    def test_bundled_plugins_load_in_fresh_interpreter_before_navigation_imports(self):
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        code = '''import gi
for name, version in [('Gtk','4.0'),('Gdk','4.0'),('Vte','3.91'),('GtkSource','5')]:
    gi.require_version(name,version)
from types import SimpleNamespace
from naui.plugin_manager import PluginManager
host=SimpleNamespace(_shutting_down=False)
host.plugins=manager=PluginManager(host)
manager.load_bundled()
assert len(manager.plugins)==7
assert manager.features['explorer_factory'].__module__.startswith('naui.plugins.base_file_explorer')
assert manager.features['navigation_factory'].__module__.startswith('naui.plugins.base_lsp')
manager.close()
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=self.temp.name,
            env=dict(os.environ, PYTHONPATH=str(root/'src')), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

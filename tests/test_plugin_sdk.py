"""Manifest, public API, validator, trust state, ZIP and fresh reload regressions."""
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import warnings

from naui.plugin_catalog import PluginCatalog
from naui.plugin_manager import PluginManager, PluginSpec
from naui.plugin_manifest import API_VERSION, capabilities, fingerprint, inspect_manifest, plugin_directory
from naui.plugin_sdk import agent_prompt, scaffold, sdk_root
from naui.plugin_storage import InstallCandidate, PluginStore
from naui.plugin_validation import PluginValidator, static_validate
from naui.workspace import Workspace
from naui.metadata import version_string
from naui_plugin_api import FileLocation

ROOT = Path(__file__).resolve().parents[1]


class PluginSDKTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='naui-sdk-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.messages, self.removals = [], []
        self.host = SimpleNamespace(_shutting_down=False, workspace=Workspace(str(self.root)),
            set_status=self.messages.append, work=lambda function, callback: callback(function(), None),
            remove_plugin_ui=lambda identity, views: self.removals.append((identity, list(views))))
        self.host.plugins = self.manager = PluginManager(self.host)
        self.addCleanup(self.manager.close)
        self.catalog = PluginCatalog(self.manager, self.root/'config/plugins')
        self.validator = PluginValidator(); self.addCleanup(self.validator.close)

    def plugin(self, permissions=('commands',), identity='hello-test'):
        root = scaffold(self.catalog.store.directory, identity, 'Hello Test', 'Add a notification command.', permissions)
        (root/'plugin.py').write_text('''from naui_plugin_api import Plugin as BasePlugin
class Plugin(BasePlugin):
    def activate(self, api):
        api.commands.add('hello', 'Hello', lambda: api.notifications.show('one'))
    def deactivate(self): pass
''')
        (root/'tests/test_plugin.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n    def test_true(self): self.assertTrue(True)\n')
        self.catalog.discover()
        return root

    def validated(self, permissions=('commands',)):
        root = self.plugin(permissions)
        result = self.catalog.validate('hello-test', execute=True)
        self.assertTrue(result.ok, result.text())
        self.catalog.enable('hello-test', permissions)
        return root

    def test_manifest_metadata_and_aliases(self):
        root = self.plugin(('commands', 'terminal', 'ssh'))
        spec, entry, warnings = inspect_manifest(root)
        self.assertEqual((spec.id, spec.version, spec.api_version, spec.source), ('hello-test', '0.1.0', 1, 'ai-created'))
        self.assertEqual(entry, root/'plugin.py')
        self.assertFalse(warnings)
        self.assertIn('terminal_execution', capabilities(spec.permissions))
        self.assertIn('remote_access', capabilities(spec.permissions))

    def test_invalid_manifest_fields_and_version(self):
        root = self.plugin()
        original = (root/'plugin.toml').read_text()
        for change in [('api_version = 1', 'api_version = 3'), ('id = "hello-test"', 'id = "../escape"'),
                       ('commands = true', 'commands = "true"'), ('commands = true', 'unknown = true'),
                       ('entrypoint = "plugin.py"', 'entrypoint = "../plugin.py"'),
                       ('name = "Hello Test"', 'name = 2')]:
            with self.subTest(change=change):
                (root/'plugin.toml').write_text(original.replace(*change))
                self.assertFalse(static_validate(root).ok)
        (root/'plugin.toml').write_text(original.replace('api_version = 1', 'api_version = true'))
        self.assertFalse(static_validate(root).ok)

    def test_unknown_manifest_fields_warn(self):
        root = self.plugin()
        path = root/'plugin.toml'; path.write_text('extra = "ignored with warning"\n' + path.read_text())
        self.assertIn('Unknown manifest field: extra', static_validate(root).warnings)

    def test_symlink_manifest_entry_and_helper_are_rejected(self):
        root = self.plugin()
        outside = self.root/'outside.py'; outside.write_text('x=1')
        for name in ('plugin.py', 'helper.py', 'plugin.toml'):
            with self.subTest(name=name):
                path = root/name; content = path.read_bytes() if path.exists() else None
                path.unlink(missing_ok=True); path.symlink_to(outside)
                self.assertFalse(static_validate(root).ok)
                path.unlink()
                if content is not None: path.write_bytes(content)

    def test_private_imports_and_attributes_and_undeclared_capabilities(self):
        root = self.plugin()
        for statement in ('from naui.gui import NauiWindow', 'import naui.backends', 'from gi.repository import Gtk',
                          'import ctypes', 'import subprocess', 'api._host.set_status("bad")',
                          'api.files.read(None, lambda *_: None)', 'api.terminals.run(device_id="local", cwd="/tmp", command=["pwd"])',
                          'import socket'):
            with self.subTest(statement=statement):
                (root/'plugin.py').write_text('def start(api):\n    ' + statement + '\n')
                result = static_validate(root)
                self.assertFalse(result.ok, statement)
                self.assertTrue(any('plugin.py:2' in error for error in result.errors), result.text())

    def test_static_validation_never_imports_code(self):
        root = self.plugin()
        marker = self.root/'executed'
        (root/'plugin.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\ndef start(api): pass\n')
        self.assertTrue(self.validator.validate(root).ok)
        self.assertFalse(marker.exists())
        self.assertTrue(self.validator.validate(root, execute=True).ok)
        self.assertTrue(marker.exists())

    def test_import_and_test_failures_report_exact_error(self):
        root = self.plugin()
        (root/'plugin.py').write_text('raise RuntimeError("load failure")\ndef start(api): pass\n')
        result = self.validator.validate(root, execute=True)
        self.assertFalse(result.ok)
        self.assertIn('load failure', result.text())
        (root/'plugin.py').write_text('def start(api): pass\n')
        (root/'tests/test_plugin.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n    def test_bad(self): self.assertEqual(1, 2)\n')
        self.assertIn('AssertionError', self.validator.validate(root, execute=True).text())

    def test_scaffold_and_generated_prompts(self):
        root = scaffold(self.root/'dev', 'hello-test', 'Hello Test', 'Add a notification', ())
        self.assertIn('*', (root/'.gitignore').read_text().splitlines())
        self.assertTrue(self.validator.validate(root, execute=True).ok)
        for mode, request, error in [('create', 'Add a notification', ''), ('modify', 'Add filtering', ''),
                                      ('modify', 'Fix errors', 'plugin.py:38 Forbidden private import')]:
            prompt = agent_prompt(root, request, (), mode=mode, errors=error)
            for value in (str(root.resolve()), str(sdk_root()), request, 'ONLY', 'DO NOT modify Naui core',
                          'PLUGIN_API_CAPABILITY_MISSING:', 'naui plugin validate', 'Requested permissions:\nnone'):
                self.assertIn(value, prompt)
            if error: self.assertIn(error, prompt)
        with self.assertRaises(FileExistsError): scaffold(root.parent, root.name, 'Again', '', ())
        with self.assertRaises(ValueError): scaffold(root.parent, '../evil', 'Bad', '', ())

    def test_permission_facades_and_zero_permission_notifications(self):
        api = self.manager.activate(PluginSpec('limited', 'Limited', frozenset()), lambda api: None, ())
        api.notifications.show('Hello'); self.assertEqual(self.messages, ['Hello'])
        for call in (lambda: api.files.read(FileLocation('local', '/tmp/a'), lambda *_: None),
                     lambda: api.terminals.run(device_id='local', cwd='/tmp', command=['pwd']),
                     lambda: api.network.request('https://example.org', lambda *_: None)):
            with self.assertRaises(PermissionError): call()
        self.assertFalse(hasattr(api, 'authentication'))

    def test_enable_disable_double_deactivate_and_callback_isolation(self):
        self.validated()
        callback = self.manager.commands[('hello-test', 'hello')][1]
        callback(); self.assertEqual(self.messages[-1], 'one')
        self.catalog.disable('hello-test'); self.catalog.disable('hello-test')
        callback(); self.assertEqual(self.messages, ['one'])
        self.assertFalse(self.manager.plugins)
        self.assertFalse(self.manager.commands)
        self.assertEqual(self.catalog.records['hello-test'].state, 'disabled')
        self.assertFalse(self.host._shutting_down)

    def test_repeated_reload_fresh_entry_and_helpers(self):
        root = self.validated()
        path = root/'plugin.py'
        path.write_text("from .helper import TEXT\ndef start(api):\n    api.commands.add('hello', 'Hello', lambda: api.notifications.show(TEXT))\n")
        helper = root/'helper.py'
        for index in range(20):
            helper.write_text(f'TEXT = "{index:02d}"\n')
            os.utime(helper, (1000000000, 1000000000))  # Same timestamp/size; cannot trust .pyc.
            self.assertTrue(self.catalog.validate('hello-test', execute=True).ok)
            self.catalog.reload('hello-test')
            self.manager.invoke('hello-test', 'hello')
            self.assertEqual(self.messages[-1], f'{index:02d}')
            self.assertEqual(len(self.manager.commands), 1)
            gc.collect()
        namespaces = [name for name in sys.modules if name.startswith('naui_user_') and '.' not in name]
        self.assertEqual(len(namespaces), 1)
        self.catalog.disable('hello-test')
        self.assertFalse([name for name in sys.modules if name.startswith('naui_user_')])

    def test_invalid_reload_preserves_working_plugin(self):
        root = self.validated()
        (root/'plugin.py').write_text('from naui.gui import NauiWindow\ndef start(api): pass\n')
        self.assertFalse(self.catalog.validate('hello-test', execute=True).ok)
        with self.assertRaises(ValueError): self.catalog.reload('hello-test')
        self.manager.invoke('hello-test', 'hello')
        self.assertEqual(self.messages[-1], 'one')

    def test_changed_code_or_permissions_cannot_auto_enable(self):
        root = self.validated()
        self.assertTrue(self.catalog.restore_check('hello-test').ok)
        (root/'plugin.py').write_text('def start(api): pass\n')
        self.assertFalse(self.catalog.restore_check('hello-test').ok)
        with self.assertRaises(ValueError): self.catalog.enable('hello-test', {'commands'})
        (root/'plugin.toml').write_text((root/'plugin.toml').read_text() + 'network = true\n')
        self.catalog.discover(); self.assertTrue(self.catalog.validate('hello-test', execute=True).ok)
        with self.assertRaises(PermissionError): self.catalog.reload('hello-test')
        self.assertIn('hello-test', self.manager.plugins)

    def test_runtime_callback_failure_does_not_stop_host(self):
        self.validated()
        api = self.manager.apis['hello-test']
        def broken(): raise RuntimeError('token=secretvalue action failed')
        api.commands.add('broken', 'Broken', broken)
        self.manager.invoke('hello-test', 'broken')
        self.assertFalse(self.host._shutting_down)
        self.assertNotIn('hello-test', self.manager.plugins)
        self.assertEqual(self.catalog.records['hello-test'].state, 'failed')
        self.assertIn('[redacted]', '\n'.join(self.manager.logs['hello-test']))
        self.assertNotIn('secretvalue', '\n'.join(self.manager.logs['hello-test']))

    def test_activation_and_deactivation_errors_are_isolated(self):
        root = self.plugin()
        (root/'plugin.py').write_text('''from naui_plugin_api import Plugin as BasePlugin
class Plugin(BasePlugin):
    def activate(self, api):
        api.commands.add('hello', 'Hello', lambda: None)
        raise ValueError('bad activate')
    def deactivate(self): raise ValueError('bad deactivate')
''')
        self.assertTrue(self.catalog.validate('hello-test', execute=True).ok)
        with self.assertRaises(ValueError): self.catalog.enable('hello-test', {'commands'})
        self.assertFalse(self.manager.commands)
        self.assertFalse(self.host._shutting_down)
        self.assertFalse(self.catalog.store.data['plugins']['hello-test']['enabled'])

    def test_settings_persistence_credential_rejection_and_state_preservation(self):
        self.validated()
        api = self.manager.apis['hello-test']
        api.settings.set('topic_filter', 'sensors/+')
        restored = PluginStore(self.catalog.store.directory)
        self.assertEqual(restored.setting('hello-test', 'topic_filter'), 'sensors/+')
        self.assertTrue(restored.data['plugins']['hello-test']['enabled'])
        for key, value in [('password', 'bad'), ('settings', {'token': 'bad'}), ('credential', 'bad')]:
            with self.assertRaises(ValueError): api.settings.set(key, value)
        restored.path.write_text('invalid json')
        bad = PluginStore(restored.directory)
        with self.assertRaises(ValueError): bad.remember('hello-test', False, [], '')
        self.assertEqual(bad.path.read_text(), 'invalid json')

    def test_folder_install_staged_without_execution_and_no_overwrite(self):
        origin = self.plugin()
        with InstallCandidate(origin) as candidate:
            destination = candidate.install(self.root/'installed')
            self.assertTrue((destination/'plugin.toml').exists())
            with self.assertRaises(ValueError): candidate.install(self.root/'installed')
        self.assertEqual((origin/'plugin.py').read_text(), (destination/'plugin.py').read_text())

    def test_external_cli_plugin_discovery_reload_is_session_only(self):
        root = self.plugin()
        external = self.root/'external'
        root.rename(external)
        self.manager.load_user(external, ['commands'])
        self.catalog.discover()
        self.assertEqual(self.catalog.records['hello-test'].directory, external)
        self.assertTrue(self.catalog.validate('hello-test', execute=True).ok)
        self.catalog.reload('hello-test')
        self.manager.invoke('hello-test', 'hello')
        self.assertEqual(self.messages[-1], 'one')
        self.catalog.disable('hello-test')
        self.assertNotIn('hello-test', self.catalog.store.data['plugins'])
        self.assertTrue(external.is_dir())

    def test_validation_cannot_change_reviewed_identity_or_permissions(self):
        root = self.plugin()
        manifest = root/'plugin.toml'
        manifest.write_text(manifest.read_text().replace('id = "hello-test"', 'id = "changed-identity"'))
        self.assertTrue(self.catalog.validate('hello-test', execute=True).ok)
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            self.catalog.enable('hello-test', ['commands'])
        self.assertFalse(self.manager.plugins)
        self.assertFalse(self.catalog.store.data['plugins'])

    def test_deactivate_cancels_only_owned_resources_and_late_callbacks(self):
        from naui.lifecycle import Sources
        from naui.plugin_api import PluginAPI
        self.host.sources = Sources()
        self.addCleanup(self.host.sources.close)
        api = PluginAPI(self.host, 'test-resources', {'commands'})
        self.manager.apis['test-resources'] = api
        called, stopped = [], []
        global_timer = self.host.sources.timeout(60000, lambda: called.append('global'))
        owned_timer = api.defer(60000, lambda: called.append('plugin'))
        api.cancel_deferred(global_timer)
        self.assertIn(global_timer, self.host.sources._ids)
        api._watches.append(SimpleNamespace(close=lambda: stopped.append('watch')))
        guarded = api.guard(lambda: called.append('late'))
        api.commands.add('hello', 'Hello', guarded)
        api.close(); api.close(); guarded()
        self.assertEqual(stopped, ['watch'])
        self.assertFalse(called)
        self.assertIn(global_timer, self.host.sources._ids)
        self.assertNotIn(owned_timer, self.host.sources._ids)
        self.assertFalse(self.manager.commands)

    def test_closed_plugin_browser_page_removal_is_exact_and_idempotent(self):
        try:
            from naui.gui import NauiWindow
        except (ImportError, ValueError) as error:
            self.skipTest(str(error))
        closed, removed = [], []
        target = SimpleNamespace(_shutting_down=True, close=lambda: closed.append('target'))
        survivor = SimpleNamespace(_shutting_down=False)
        pages = [target, survivor]
        host = SimpleNamespace(_shutting_down=False, viewers=[target, survivor],
            notebook=SimpleNamespace(page_num=lambda view: pages.index(view),
                remove_page=lambda index: removed.append(pages.pop(index))))
        NauiWindow.remove_plugin_view(host, target)
        NauiWindow.remove_plugin_view(host, target)
        self.assertEqual(closed, ['target'])
        self.assertEqual(removed, [target])
        self.assertEqual(pages, [survivor])
        self.assertEqual(host.viewers, [survivor])
        self.assertFalse(host._shutting_down)

    def test_zip_install_and_traversal_symlink_duplicates_rejected(self):
        origin = self.plugin()
        archive = self.root/'plugin.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for path in origin.rglob('*'):
                if path.is_file(): output.write(path, 'wrapper/'+str(path.relative_to(origin)))
        candidate = InstallCandidate(archive, zipped=True)
        self.addCleanup(candidate.close)
        self.assertEqual(candidate.spec.id, 'hello-test')
        destination = candidate.install(self.root/'from-zip')
        self.assertTrue(static_validate(destination).ok)
        for name in ('../../evil.txt', '/tmp/evil', 'C:/evil', 'a\\..\\evil', '.'):
            with self.subTest(name=name):
                with zipfile.ZipFile(archive, 'w') as output: output.writestr(name, 'evil')
                with self.assertRaises(ValueError): InstallCandidate(archive, zipped=True)
        with zipfile.ZipFile(archive, 'w') as output:
            entry = zipfile.ZipInfo('link'); entry.external_attr = (0o120777 << 16)
            output.writestr(entry, '/tmp/evil')
        with self.assertRaises(ValueError): InstallCandidate(archive, zipped=True)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('duplicate.txt', 'one'); output.writestr('duplicate.txt', 'two')
        with self.assertRaisesRegex(ValueError, 'Unsafe ZIP'): InstallCandidate(archive, zipped=True)

    def test_uninstall_removes_only_managed_plugin_and_preserves_settings(self):
        root = self.validated()
        self.manager.apis['hello-test'].settings.set('color', 'blue')
        unrelated = self.root/'keep'; unrelated.write_text('keep')
        self.catalog.uninstall('hello-test')
        self.assertFalse(root.exists()); self.assertEqual(unrelated.read_text(), 'keep')
        self.assertEqual(self.catalog.store.setting('hello-test', 'color'), 'blue')
        self.assertFalse(self.manager.commands)

    def test_validator_cancellation_reaps_subprocess(self):
        import threading
        root = self.plugin()
        marker = self.root/'ready'
        (root/'plugin.py').write_text(f'import time\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\ntime.sleep(30)\ndef start(api): pass\n')
        result = []
        thread = threading.Thread(target=lambda: result.append(self.validator.validate(root, execute=True)))
        thread.start()
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(marker.exists())
        self.validator.close(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result[0].ok)
        self.assertFalse(self.validator._processes)

    def test_validator_launch_failure_reports_structured_error(self):
        root = self.plugin()
        with patch('naui.plugin_validation.subprocess.Popen', side_effect=OSError('launch unavailable')):
            result = self.validator.validate(root, execute=True)
        self.assertFalse(result.ok)
        self.assertIn('Unable to start plugin validator: launch unavailable', result.text())
        self.assertFalse(result.executed)

    def test_validator_rejects_invalid_lifecycle_signature(self):
        root = self.plugin()
        for code in ('def start(): pass\n',
                     'class Plugin:\n    def activate(self): pass\n    def deactivate(self): pass\n',
                     'class Plugin:\n    def activate(self, api): pass\n    def deactivate(self, api): pass\n'):
            with self.subTest(code=code):
                (root/'plugin.py').write_text(code)
                result = self.validator.validate(root, execute=True)
                self.assertFalse(result.ok, result.text())
                self.assertIn('TypeError', result.text())

    def test_cli_without_display_and_api_version(self):
        environment = dict(os.environ, PYTHONPATH=str(ROOT/'src'))
        environment.pop('DISPLAY', None); environment.pop('WAYLAND_DISPLAY', None)
        for arguments, expected in [(['plugin', 'api-version'], str(API_VERSION)), (['--version'], version_string())]:
            result = subprocess.run([str(ROOT/'naui'), *arguments], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        root = self.plugin()
        result = subprocess.run([str(ROOT/'naui'), 'plugin', 'validate', str(root), '--json'], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['executed'])
        with patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root/'xdg')):
            self.assertEqual(plugin_directory(), self.root/'xdg/naui/plugins')

    def test_reference_examples_validate_and_test(self):
        for identity in ('hello-world', 'sidebar-panel', 'file-action', 'terminal-command'):
            with self.subTest(identity=identity):
                result = self.validator.validate(ROOT/'examples/plugins'/identity, execute=True)
                self.assertTrue(result.ok, result.text())
                self.assertTrue(result.executed)

"""Agent providers/recovery with harmless executables and real owned POSIX PTYs."""
import json
import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from naui.ai_agents import (AiAgentManager, AiAgentSettings, ApiAgentProvider, ClaudeCliProvider,
    CodexCliProvider, CustomCliProvider, agent_environment, plugin_snapshot, review_changes, terminal_prompt)
from naui.plugin_sdk import scaffold, agent_prompt
from naui.plugin_storage import PluginStore
from naui.plugin_catalog import PluginCatalog
from naui.plugin_manager import PluginManager
from naui.plugin_manifest import fingerprint
from naui.protocol import OscParser, new_token
from naui.pty_bridge import PtyBridge

ROOT = Path(__file__).resolve().parents[1]


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='naui-ai-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'runtime').mkdir(mode=0o700)
        environment = patch.dict(os.environ, XDG_RUNTIME_DIR=str(self.root/'runtime'))
        environment.start(); self.addCleanup(environment.stop)
        self.plugins = self.root/'config/naui/plugins'
        self.plugin = scaffold(self.plugins, 'hello-agent', 'Hello Agent', 'Notify explicitly.', [])
        self.agent = self.root/'mock-agent'
        self.agent.write_text('#!' + sys.executable + '\n' + '''import json,os,sys,time
from pathlib import Path
mode = sys.argv[1]
prompt = sys.stdin.read() if mode == 'stdin' else Path(sys.argv[2]).read_text() if mode == 'file' else ''
Path('result.json').write_text(json.dumps({'cwd':os.getcwd(), 'prompt':prompt, 'env':dict(os.environ), 'tty':os.isatty(1)}))
if mode == 'wait': time.sleep(30)
if mode == 'sdk':
    import subprocess
    Path('sdk-paths.json').write_text(subprocess.check_output(['naui','plugin','paths'], text=True))
''')
        self.agent.chmod(0o700)
        self.settings = AiAgentSettings(self.root/'config/naui/ai-agents.json')
        self.configure('stdin', ['stdin'])
        self.bridges, self.sent, self.stopped = {}, [], []
        def launch(task, command):
            env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), NAUI_TOKEN='do-not-forward',
                OPENAI_API_KEY='unrelated-do-not-forward', SSH_AUTH_SOCK='/not-a-real-agent')
            bridge = PtyBridge(command, env, task.cwd, OscParser(new_token(), lambda _: None))
            self.bridges[task.id] = bridge
            return task.id, bridge.process
        def stop(identity):
            self.stopped.append(identity); self.bridges[identity].close()
            return True
        self.manager = AiAgentManager(self.plugins, launch, stop, lambda identity, data: self.sent.append((identity, data)), settings=self.settings)
        self.addCleanup(self.cleanup)
    def cleanup(self):
        self.manager.close()
        for bridge in self.bridges.values(): bridge.close()
        for task in self.manager.tasks.values(): task.cleanup()
    def configure(self, delivery, arguments):
        self.settings.save({'schema':1, 'default_provider':'custom', 'default_model':'',
            'custom':{'name':'Mock Agent', 'executable':str(self.agent), 'arguments':arguments, 'delivery':delivery}})
    def wait_task(self, identity):
        task = self.manager.tasks[identity]
        self.assertEqual(task.process.wait(5), 0)
        self.bridges[identity].close()
        self.assertTrue(self.manager.terminal_finished(task.terminal_id, 0))
        return task
    def test_provider_detection_defaults_and_arguments(self):
        with patch('naui.ai_agents.shutil.which', return_value=None):
            for provider in (CodexCliProvider(), ClaudeCliProvider()):
                self.assertFalse(provider.available()); self.assertIn('not detected', provider.status())
                self.assertIsNone(provider.supported_models())
        with patch('naui.ai_agents.executable_path', return_value='/mock/program'):
            command, mode = CodexCliProvider().prepare('/plugin', '/private/prompt', 'selected-model')
            self.assertEqual(mode, 'prompt-file'); self.assertIn('workspace-write', command)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
            self.assertEqual(command[command.index('--model')+1], 'selected-model')
            command, _ = ClaudeCliProvider().prepare('/plugin', '/private/prompt')
            self.assertNotIn('--model', command)
        self.assertFalse(ApiAgentProvider().available())
        self.assertIn('not collected', ApiAgentProvider().status())
    def test_custom_settings_validation_no_secret_storage(self):
        valid = dict(self.settings.data['custom'])
        for changes in ({'delivery':'bad'}, {'arguments':'shell string'}, {'arguments':['--token','secret-value']},
                        {'delivery':'prompt-file', 'arguments':['no-placeholder']}, {'password':'secret'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): CustomCliProvider({**valid, **changes})
        before = self.settings.path.read_bytes()
        with self.assertRaises(ValueError): self.settings.save({**self.settings.data, 'token':'no'})
        with self.assertRaises(ValueError): self.settings.save({**self.settings.data, 'custom':{**valid, 'executable':'missing-test-agent-xyz'}})
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(AiAgentSettings(self.settings.path).data, self.settings.data)
    def test_allowlist_filters_authentication_and_provider_tokens(self):
        data = agent_environment({'PATH':'/bin', 'HOME':'/home/user', 'NAUI_LOCAL_KEY':'secret',
            'SSH_AUTH_SOCK':'socket', 'ANTHROPIC_API_KEY':'secret', 'OPENAI_API_KEY':'secret', 'SUDO_ASKPASS':'secret'})
        self.assertEqual(data, {'PATH':'/bin', 'HOME':'/home/user'})
    def test_real_pty_stdin_cwd_completion_and_double_cleanup(self):
        identity = self.manager.start_task('custom', self.plugin, 'private prompt')
        task = self.wait_task(identity)
        result = json.loads((self.plugin/'result.json').read_text())
        self.assertEqual(result['cwd'], str(self.plugin)); self.assertEqual(result['prompt'], 'private prompt')
        self.assertTrue(result['tty'])
        for key in ('NAUI_TOKEN', 'NAUI_LOCAL_KEY', 'SSH_AUTH_SOCK', 'OPENAI_API_KEY'): self.assertNotIn(key, result['env'])
        self.assertEqual(task.state, 'completed'); self.assertTrue(task.cleaned)
        self.assertFalse(Path(task.runtime.name).exists())
        task.cleanup(); self.manager.stop_task(identity); self.manager.close(); self.manager.close()
        self.assertFalse(self.stopped)
    def test_prompt_file_delivery_and_terminal_send_are_explicit(self):
        self.configure('prompt-file', ['file', '{prompt_file}'])
        identity = self.manager.start_task('custom', self.plugin, 'file prompt')
        self.wait_task(identity)
        self.assertEqual(json.loads((self.plugin/'result.json').read_text())['prompt'], 'file prompt')
        self.configure('terminal', ['wait'])
        identity = self.manager.start_task('custom', self.plugin, 'terminal prompt')
        self.assertFalse(self.sent)
        self.manager.send_prompt(identity)
        self.assertIn(b'terminal prompt', self.sent[0][1])
        with self.assertRaises(ValueError): self.manager.send_prompt(identity)
        self.manager.stop_task(identity); self.manager.stop_task(identity)
        self.assertEqual(self.stopped, [identity])
    def test_terminal_prompt_cannot_break_out_of_bracketed_paste(self):
        self.assertEqual(terminal_prompt('Unicode õ\r\ntext\t'.encode()), 'Unicode õ\ntext\t'.encode())
        for data in (b'\x1b[201~injected', b'\x03', b'\rcommand', b'\x7f'):
            with self.assertRaises(ValueError): terminal_prompt(data)
        self.configure('terminal', ['wait'])
        with self.assertRaisesRegex(ValueError, 'terminal control'):
            self.manager.start_task('custom', self.plugin, '\x1b[201~injected')
        self.assertFalse(self.bridges)
    def test_target_boundary_missing_provider_duplicate_and_launch_failure(self):
        with self.assertRaises(ValueError): self.manager.start_task('custom', ROOT, 'bad target')
        with self.assertRaises(ValueError): self.manager.start_task('missing', self.plugin, 'test')
        self.configure('stdin', ['wait'])
        identity = self.manager.start_task('custom', self.plugin, 'test')
        with self.assertRaises(ValueError): self.manager.start_task('custom', self.plugin, 'duplicate')
        self.manager.stop_task(identity)
        with patch.object(self.manager, 'launch', side_effect=OSError('launch failed')):
            with self.assertRaises(OSError): self.manager.start_task('custom', self.plugin, 'test')
        task = list(self.manager.tasks.values())[-1]
        self.assertEqual(task.state, 'failed'); self.assertTrue(task.cleaned)
    def test_cancelled_but_pending_close_is_owned_until_shutdown(self):
        self.configure('stdin', ['wait'])
        identity = self.manager.start_task('custom', self.plugin, 'test')
        requests = []
        real_stop = self.manager.stop_terminal
        self.manager.stop_terminal = requests.append  # Model deferred GUI disposal.
        self.manager.stop_task(identity)
        self.assertFalse(self.manager.tasks[identity].cleaned)
        with self.assertRaisesRegex(ValueError, 'still stopping'):
            self.manager.start_task('custom', self.plugin, 'another')
        self.manager.stop_terminal = real_stop
        self.manager.close(); self.manager.close()
        self.assertTrue(self.manager.tasks[identity].cleaned)
        self.assertEqual(self.stopped, [identity])
    def test_nonzero_exit_is_task_failure_not_application_failure(self):
        self.agent.write_text('#!' + sys.executable + '\nraise SystemExit(7)\n')
        identity = self.manager.start_task('custom', self.plugin, 'failure')
        task = self.manager.tasks[identity]
        self.assertEqual(task.process.wait(5), 7)
        self.bridges[identity].close()
        self.manager.terminal_finished(identity, 7)
        self.assertEqual(task.state, 'failed'); self.assertIn('7', task.error)
        self.assertTrue(task.cleaned); self.assertFalse(self.manager.closed)
    def test_exited_child_waits_for_core_pty_cleanup_confirmation(self):
        identity = self.manager.start_task('custom', self.plugin, 'test')
        task = self.manager.tasks[identity]
        self.assertEqual(task.process.wait(5), 0)
        self.manager.stop_terminal = lambda _: False
        self.manager.stop_task(identity)
        self.assertFalse(task.cleaned)
        with self.assertRaisesRegex(ValueError, 'still stopping'):
            self.manager.start_task('custom', self.plugin, 'retry')
        self.bridges[identity].close()
        self.manager.terminal_finished(identity, 0, removed=True)
        self.assertTrue(task.cleaned)
    def test_stop_failure_is_isolated_and_retryable(self):
        self.configure('stdin', ['wait'])
        identity = self.manager.start_task('custom', self.plugin, 'test')
        stop = self.manager.stop_terminal
        with patch.object(self.manager, 'stop_terminal', side_effect=OSError('test failure')):
            with self.assertRaises(OSError): self.manager.stop_task(identity)
        self.assertEqual(self.manager.tasks[identity].state, 'running')
        self.assertFalse(self.manager.closed)
        self.manager.stop_terminal = stop
        self.manager.stop_task(identity)
        self.assertTrue(self.manager.tasks[identity].cleaned)
    def test_cancel_one_agent_preserves_other_session_and_fifty_cycles(self):
        self.configure('stdin', ['wait'])
        other = scaffold(self.plugins, 'other-agent', 'Other Agent', '', [])
        survivor = self.manager.start_task('custom', other, 'survivor')
        for _ in range(50):
            (self.plugin/'result.json').unlink(missing_ok=True)
            identity = self.manager.start_task('custom', self.plugin, 'stress')
            deadline = time.monotonic() + 5
            while not (self.plugin/'result.json').exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue((self.plugin/'result.json').exists(), 'Agent did not reach its own code')
            self.manager.stop_task(identity)
            self.manager.terminal_finished(identity, self.bridges[identity].process.returncode, removed=True)
            gc.collect()
            self.assertIsNone(self.manager.tasks[survivor].process.poll())
            self.assertEqual(self.manager.tasks[identity].state, 'cancelled')
            self.assertTrue(self.manager.tasks[identity].cleaned)
        self.manager.stop_task(survivor)
    def test_task_cli_uses_launched_build_not_an_old_path_installation(self):
        self.configure('stdin', ['sdk'])
        identity = self.manager.start_task('custom', self.plugin, 'sdk probe')
        self.wait_task(identity)
        paths = json.loads((self.plugin/'sdk-paths.json').read_text())
        self.assertEqual(paths['installation'], str(ROOT))
        self.assertEqual(paths['api_version'], 1)
    def test_changes_snapshot_and_master_prompt(self):
        before = plugin_snapshot(self.plugin)
        (self.plugin/'README.md').write_text('updated\n'); (self.plugin/'new.py').write_text('pass\n')
        (self.plugin/'tests/test_plugin.py').unlink()
        changes = review_changes(self.plugin, before)
        for value in ('Modified: README.md', 'Created: new.py', 'Deleted: tests/test_plugin.py', '--- before/README.md'):
            self.assertIn(value, changes)
        prompt = agent_prompt(self.plugin, 'requested behavior', [])
        for value in ('naui --version', 'naui plugin api-version', 'naui plugin sdk-path', 'naui plugin directory',
                      'You may modify ONLY:', 'PLUGIN_API_CAPABILITY_MISSING:', str(self.plugin)):
            self.assertIn(value, prompt)
    def test_cli_paths_validate_enable_disable_uninstall_without_display(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), XDG_CONFIG_HOME=str(self.root/'config'))
        def run(*arguments): return subprocess.run([str(ROOT/'naui'), 'plugin', *arguments], env=env, capture_output=True, text=True)
        paths = json.loads(run('paths').stdout)
        self.assertEqual(paths['plugins'], str(self.plugins))
        self.assertEqual(run('validate', 'hello-agent').returncode, 0)
        self.assertEqual(run('enable', 'hello-agent').returncode, 1)
        self.assertEqual(run('enable', 'hello-agent', '--trust').returncode, 0)
        self.assertIn('enabled for next launch', run('list').stdout)
        self.assertEqual(run('disable', 'hello-agent').returncode, 0)
        self.assertEqual(run('disable', 'hello-agent').returncode, 0)
        self.assertEqual(run('uninstall', 'hello-agent').returncode, 1)
        self.assertEqual(run('uninstall', 'hello-agent', '--yes').returncode, 0)
        self.assertFalse(self.plugin.exists())
        self.assertEqual(run('disable', '../escape').returncode, 1)
    def test_safe_mode_and_interrupted_startup_recovery(self):
        host = SimpleNamespace(_shutting_down=False)
        manager = PluginManager(host); self.addCleanup(manager.close)
        catalog = PluginCatalog(manager, self.plugins, safe_mode=True)
        catalog.store.remember('hello-agent', True, [], fingerprint(self.plugin))
        before = catalog.store.path.read_bytes()
        self.assertEqual(catalog.restore_candidates(), [])
        with self.assertRaisesRegex(ValueError, 'SAFE MODE'): catalog.enable('hello-agent', [])
        self.assertEqual(catalog.store.path.read_bytes(), before)
        catalog.safe_mode = False
        self.assertEqual(catalog.restore_candidates(), ['hello-agent'])
        catalog.startup_begin('hello-agent')
        catalog.discover(); self.assertEqual(catalog.records['hello-agent'].state, 'failed')
        self.assertEqual(catalog.restore_candidates(), [])
        catalog.startup_finished('hello-agent', failed=True)
        self.assertEqual(catalog.restore_candidates(), [])
        self.assertEqual(PluginStore(self.plugins).data['plugins']['hello-agent']['startup_failures'], 1)
        catalog.disable('hello-agent'); catalog.disable('hello-agent')
        self.assertFalse(catalog.store.data['plugins']['hello-agent']['enabled'])
    def test_explicit_enable_persists_crash_marker_before_live_import(self):
        host = SimpleNamespace(_shutting_down=False, remove_plugin_ui=lambda *_: None)
        manager = host.plugins = PluginManager(host)
        self.addCleanup(manager.close)
        catalog = PluginCatalog(manager, self.plugins)
        self.assertTrue(catalog.validate('hello-agent', execute=True).ok)
        load = manager.load_user
        def check_before_load(*args):
            saved = PluginStore(self.plugins).data['plugins']['hello-agent']
            self.assertTrue(saved['startup_pending'])
            return load(*args)
        with patch.object(manager, 'load_user', side_effect=check_before_load): catalog.enable('hello-agent', [])
        self.assertFalse(PluginStore(self.plugins).data['plugins']['hello-agent']['startup_pending'])
        catalog.disable('hello-agent'); catalog.disable('hello-agent')
    def test_ai_exit_never_enters_local_last_shell_exit_confirmation(self):
        try:
            import gi
            for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('Vte', '3.91'), ('GtkSource', '5')]: gi.require_version(name, version)
            from naui.gui import NauiWindow
        except (ImportError, ValueError) as error: self.skipTest(str(error))
        calls, statuses = [], []
        host = SimpleNamespace(_shutting_down=False, runtimes={'ai':SimpleNamespace(closed=False, closing=False)},
            terminals={'ai':SimpleNamespace(returncode=0)}, ai_agents=SimpleNamespace(terminal_finished=lambda *args: True),
            sources=SimpleNamespace(idle=lambda *args: calls.append(args)), close_terminal=lambda *args: None,
            set_status=statuses.append)
        NauiWindow.terminal_exit(host, 'ai')
        self.assertTrue(host.runtimes['ai'].closed); self.assertFalse(host._shutting_down)
        self.assertFalse(calls)  # Finished task retains its read-only output tab.

"""Pure exit decisions + headless native keyboard signals/controller ownership."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from naui.session_lifecycle import classify_exit
from naui.workspace import Workspace, TerminalState
from test_sync_controller import ControllerSurface, NauiWindow


class ExitPolicyTests(unittest.TestCase):
    def test_remote_exit_never_exits_application(self):
        for status in (0, 1, -15):
            self.assertEqual(classify_exit(remote=True, returncode=status, other_terminals=False).action, 'close_tab')

    def test_last_local_normal_exit_or_eof_requires_confirmation(self):
        self.assertEqual(classify_exit(remote=False, returncode=0, other_terminals=False).action, 'confirm_exit')

    def test_unexpected_shell_exit_does_not_request_application_exit(self):
        for status, text in ((1, 'Exit code: 1'), (-9, 'Signal 9')):
            decision = classify_exit(remote=False, returncode=status, other_terminals=False)
            self.assertEqual(decision.action, 'restart_or_close')
            self.assertIn(text, decision.message)

    def test_secondary_exit_closes_only_current_tab(self):
        self.assertEqual(classify_exit(remote=False, returncode=0, other_terminals=True).action, 'close_tab')


class FakeDialog:
    def __init__(self, **kwargs):
        self.title = kwargs['title']
        self.handlers = {}
        self.controls = []
        self.destroyed = False
    def connect(self, signal, callback):
        identity = len(self.handlers) + 1
        self.handlers[identity] = signal, callback
        return identity
    def handler_is_connected(self, identity): return identity in self.handlers
    def disconnect(self, identity): del self.handlers[identity]
    def get_content_area(self): return SimpleNamespace(append=lambda widget: None)
    def add_button(self, label, answer): pass
    def set_default_response(self, answer): self.default = answer
    def add_controller(self, controller): self.controls.append(controller)
    def present(self): pass
    def destroy(self): self.destroyed = True
    def response(self, answer):
        for signal, callback in list(self.handlers.values()):
            if signal == 'response': callback(self, answer)


@unittest.skipIf(NauiWindow is None, 'GTK bindings unavailable')
class NativeExitControllerTests(unittest.TestCase):
    def setUp(self):
        from naui.lifecycle import Signals, Sources
        from gi.repository import Gdk, Gtk
        self.Gdk, self.Gtk = Gdk, Gtk
        self.events = []
        workspace = Workspace('/tmp')
        runtime = TerminalState('local-terminal', 'local', 'local', {})
        runtime.closed = True
        self.window = SimpleNamespace(_shutting_down=False, workspace=workspace, signals=Signals(), sources=Sources(),
            dialog=None, runtimes={runtime.id: runtime}, terminals={runtime.id: SimpleNamespace(returncode=0)},
            set_status=lambda text: self.events.append(('status', text)),
            close_terminal=lambda identity, force: self.events.append(('close-tab', identity)),
            create_terminal=lambda device, cwd: self.events.append(('restart', device.id, cwd)),
            close=lambda: self.events.append(('close-app',)))
        self.window.after_terminal_exit = NauiWindow.after_terminal_exit.__get__(self.window)
        self.addCleanup(self.window.sources.close)
        self.addCleanup(self.window.signals.close)
        dialog_patch = patch('naui.gui.Gtk.Dialog', FakeDialog)
        label_patch = patch('naui.gui.Gtk.Label', lambda **kwargs: object())
        dialog_patch.start(); label_patch.start()
        self.addCleanup(dialog_patch.stop); self.addCleanup(label_patch.stop)

    def test_n_and_escape_restart_without_exiting_gui(self):
        for key in (self.Gdk.KEY_n, self.Gdk.KEY_Escape):
            self.events.clear()
            self.window.after_terminal_exit('local-terminal')
            dialog = self.window.dialog
            self.assertEqual(dialog.title, 'Exit Naui?')
            self.assertTrue(dialog.controls[0].emit('key-pressed', key, 0, 0))
            self.assertTrue(dialog.destroyed)
            self.assertEqual([event[0] for event in self.events], ['close-tab', 'restart'])
            self.assertIsNone(self.window.dialog)
            self.assertFalse(self.window.signals._handlers)

    def test_y_uses_normal_close_path_once(self):
        self.window.after_terminal_exit('local-terminal')
        dialog = self.window.dialog
        self.assertTrue(dialog.controls[0].emit('key-pressed', self.Gdk.KEY_y, 0, 0))
        self.assertEqual(self.events, [('close-app',)])
        self.assertTrue(dialog.destroyed)

    def test_window_close_on_confirmation_is_safe_restart(self):
        self.window.after_terminal_exit('local-terminal')
        self.window.dialog.response(self.Gtk.ResponseType.DELETE_EVENT)
        self.assertEqual([event[0] for event in self.events], ['close-tab', 'restart'])

    def test_failed_child_shows_restart_and_close_tab_not_application_exit(self):
        self.window.terminals['local-terminal'].returncode = -9
        self.window.after_terminal_exit('local-terminal')
        dialog = self.window.dialog
        self.assertEqual(dialog.title, 'Terminal exited')
        dialog.response(self.Gtk.ResponseType.ACCEPT)
        self.assertEqual(self.events, [('close-tab', 'local-terminal')])

    def test_remote_child_exits_only_its_terminal_without_gui_confirmation(self):
        device = self.window.workspace.add_ssh('server')
        runtime = self.window.runtimes['local-terminal']
        runtime.base_device_id = runtime.device_id = device.id
        self.window.after_terminal_exit(runtime.id)
        self.assertIsNone(self.window.dialog)
        self.assertEqual(self.events[-1], ('close-tab', runtime.id))
        self.assertNotIn(('close-app',), self.events)


@unittest.skipIf(NauiWindow is None, 'GTK bindings unavailable')
class DisconnectControllerTests(unittest.TestCase):
    def test_disconnect_cancels_only_owned_device_and_keeps_local_returned_shell_and_editors(self):
        from naui.protocol import sign, new_token
        window = ControllerSurface()
        device = window.workspace.add_ssh('server-a')
        other = window.workspace.add_ssh('server-b')
        device.state = 'connecting'; window.add_panel(device); window.add_panel(other)
        runtime = TerminalState('remote-terminal', device.id, device.id,
            {None: {'backend': device.filesystem_backend, 'device_id': device.id, 'state': 'connecting'}})
        key = new_token()
        window.runtimes[runtime.id] = runtime
        device.terminal_sessions.extend([runtime.id, 'terminal-local'])  # Historical SSH membership.
        window.terminals[runtime.id] = SimpleNamespace(local_key=key, closed=False)
        window.terminal_message(runtime.id, sign({'action': 'session-start', 'session': 'ssh-a',
                                                 'host': 'server-a', 'options': []}, key))
        window.terminal_message(runtime.id, {'action': 'session-ready', 'location': 'remote', 'session': 'ssh-a'})
        cancelled, forgotten, closed, marked = [], [], [], []
        window.transfers = SimpleNamespace(cancel_device=cancelled.append)
        window.authentication = SimpleNamespace(forget_device=forgotten.append)
        window.close_terminal = lambda identity, force: (closed.append(identity), setattr(window.runtimes[identity], 'closed', True))
        remote_editor = SimpleNamespace(device_id=device.id, dirty=True, text='unsaved',
                                        mark_disconnected=lambda *args: marked.append(device.id))
        local_editor = SimpleNamespace(device_id='local', dirty=True, text='local unsaved',
                                       mark_disconnected=lambda *args: self.fail('Local editor disconnected'))
        window.viewers.extend([remote_editor, local_editor])
        NauiWindow.disconnect_device(window, device.id)
        self.assertEqual(set(cancelled), {device.id})
        self.assertEqual(forgotten, [device.id])
        self.assertEqual(closed, [runtime.id])
        self.assertEqual(device.state, 'disconnected')
        self.assertFalse(device.filesystem_backend.connected)
        self.assertFalse(window.runtimes['terminal-local'].closed)
        self.assertTrue(remote_editor.dirty)
        self.assertEqual(remote_editor.text, 'unsaved')
        self.assertEqual(local_editor.text, 'local unsaved')
        self.assertIn(device.id, marked)
        self.assertIn('local', window.workspace_sidebar.explorers)
        self.assertIn(device.id, window.workspace.devices)
        self.assertIn(other.id, window.workspace.devices)
        self.assertFalse(window._shutting_down)

    def test_absent_final_terminal_properties_and_shutdown_remain_idempotent(self):
        from naui.lifecycle import Signals
        window = ControllerSurface()
        window.active_terminal_id = None
        window.terminals.clear()
        window.at_prompt = False
        window.active_session = None
        window.navigation_pending = None
        window.navigation_timeout = None
        self.assertIsNone(window.runtime)
        self.assertFalse(window.navigate_terminal('/tmp', None))
        events = []
        window.workspace_dirty = False
        window.pending_paths = set()
        window.dialog = None
        window.signals = Signals()
        window.sources.close = lambda: events.append('sources')
        window.file_work = SimpleNamespace(stop=lambda: events.append('files-stop'), join=lambda: events.append('files-join'))
        window.preview_cache = SimpleNamespace(cancel=lambda: events.append('preview-stop'), close=lambda: events.append('preview-close'))
        window.workspace_sidebar.close = lambda: events.append('sidebar')
        window.transfers = SimpleNamespace(close=lambda: events.append('transfers-stop'), join=lambda: events.append('transfers-join'))
        window.authentication = SimpleNamespace(close=lambda: events.append('auth-stop'))
        window.plugins = SimpleNamespace(close=lambda: events.append('plugins-stop'))
        window.transfer_panel = SimpleNamespace(close=lambda: events.append('panel-stop'))
        NauiWindow.shutdown(window); NauiWindow.shutdown(window)
        self.assertTrue(window._shutting_down)
        self.assertEqual(events, ['sources', 'panel-stop', 'plugins-stop', 'transfers-stop', 'auth-stop',
                                  'files-stop', 'preview-stop', 'sidebar', 'files-join', 'transfers-join', 'preview-close'])

    def test_disconnect_nested_ssh_keeps_outer_local_pty_and_returns_its_context(self):
        from naui.protocol import sign
        window = ControllerSurface()
        window.local('cwd', path='/tmp/local-project', home='/home/test')
        window.local('session-start', session='nested', host='server', options=[])
        device = window.workspace.devices[window.session_devices['nested']]
        window.terminal_message('terminal-local', {'action': 'session-ready', 'location': 'remote', 'session': 'nested'})
        stopped = []
        window.terminals['terminal-local'].bridge = SimpleNamespace(stop_ssh_foreground=stopped.append)
        window.transfers = SimpleNamespace(cancel_device=lambda identity: None)
        window.authentication = SimpleNamespace(forget_device=lambda identity: None)
        window.close_terminal = lambda *_: self.fail('Nested SSH disconnect must keep the outer local terminal')
        NauiWindow.disconnect_device(window, device.id)
        runtime = window.runtimes['terminal-local']
        self.assertEqual(stopped, ['server'])
        self.assertFalse(runtime.closed)
        self.assertEqual(runtime.device_id, 'local')
        self.assertIsNone(runtime.active_session)
        self.assertEqual(runtime.contexts[None]['path'], '/tmp/local-project')
        self.assertIn('terminal-local', window.terminals)
        self.assertEqual(device.state, 'disconnected')
        self.assertNotIn('terminal-local', device.terminal_sessions)


class OwnedForegroundTests(unittest.TestCase):
    def test_only_verified_ssh_wrapper_group_is_signalled(self):
        import signal
        from unittest.mock import mock_open
        from naui.pty_bridge import PtyBridge
        groups, signalled = {100, 200}, []
        bridge = SimpleNamespace(closed=False, process=SimpleNamespace(pid=100, poll=lambda: None), shell_master=1,
            _session_groups=lambda: groups.copy(),
            _signal_group=lambda group, sig: (signalled.append((group, sig)), groups.discard(group)))
        with patch('naui.pty_bridge.os.tcgetpgrp', return_value=200), patch('builtins.open',
                mock_open(read_data=b'/usr/bin/python3\0-m\0naui.shell\0ssh\0server\0')):
            self.assertTrue(PtyBridge.stop_ssh_foreground(bridge, 'server'))
        self.assertEqual(signalled, [(200, signal.SIGTERM)])
        self.assertIn(100, groups)

    def test_changed_or_idle_local_foreground_never_gets_killed(self):
        from unittest.mock import mock_open
        from naui.pty_bridge import PtyBridge
        signalled = []
        bridge = SimpleNamespace(closed=False, process=SimpleNamespace(pid=100, poll=lambda: None), shell_master=1,
            _session_groups=lambda: {100, 200}, _signal_group=lambda *args: signalled.append(args))
        with patch('naui.pty_bridge.os.tcgetpgrp', return_value=200), patch('builtins.open',
                mock_open(read_data=b'/usr/bin/python3\0server.py\0')):
            with self.assertRaises(RuntimeError):
                PtyBridge.stop_ssh_foreground(bridge, 'server')
        with patch('naui.pty_bridge.os.tcgetpgrp', return_value=100):
            self.assertFalse(PtyBridge.stop_ssh_foreground(bridge, 'server'))
        self.assertEqual(signalled, [])

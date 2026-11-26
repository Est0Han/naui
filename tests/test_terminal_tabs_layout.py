"""Per-tab disposal ordering, real PTY independence, SSH leases and UI prefs.

Native VtePty/GLib are real; widget surfaces are stubbed without a display.
See terminal_tabs_smoke.py for the actual rendered VTE/tab-close reproducer.
"""
import os
import gc
import select
import time
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_sync_controller import ControllerSurface, NauiWindow
from naui.workspace import RemoteDeviceBackend, TerminalState
from naui.protocol import new_token, OscParser, sign
from naui.pty_bridge import PtyBridge
from naui.session_lifecycle import TerminalLifecycle
from naui.ui_settings import UISettings, DEFAULTS
from test_workspace import RootedTransport
from naui.lifecycle import Sources, Signals
from naui.terminal import TerminalPane
from gi.repository import GLib, Vte


def drain():
    context = GLib.MainContext.default()
    errors = []
    with patch.object(sys, 'excepthook', lambda kind, value, traceback: errors.append(value)):
        for _ in range(100):
            if not context.pending(): break
            context.iteration(False)
        else: raise AssertionError('Sources did not quiesce')
    if errors: raise errors[0]


class TabDisposalTests(unittest.TestCase):
    def surface(self, count):
        window = ControllerSurface()
        window.sources = Sources()
        window.signals = Signals()
        self.addCleanup(window.sources.close)
        self.addCleanup(window.signals.close)
        window.runtimes, window.terminals, window.terminal_labels = {}, {}, {}
        window.pending_runs, window.running_runs = {}, {}
        closed, removed = [], []
        window.code_navigation = SimpleNamespace(remove_terminal=lambda identity: removed.append(identity))
        window.last_terminal_closed = lambda: None
        window.confirm_action = lambda *args: self.fail('Idle terminal must not ask for confirmation')
        for method in ('close_terminal', 'dispose_terminal'):
            setattr(window, method, getattr(NauiWindow, method).__get__(window))
        device = window.workspace.devices['local']
        device.terminal_sessions.clear()
        pages = []
        for index in range(count):
            identity = str(index)
            window.runtimes[identity] = TerminalState(identity, 'local', 'local', {})
            terminal = SimpleNamespace(closed=False, tab_close_button=object(),
                bridge=SimpleNamespace(foreground_description=lambda: None))
            def close(identity=identity, terminal=terminal):
                self.assertTrue(window.runtimes[identity].closing)
                terminal.closed = True
                closed.append(identity)
            terminal.close = close
            pages.append(terminal)
            window.terminals[identity] = terminal
            window.terminal_labels[identity] = SimpleNamespace(set_text=lambda text: None)
            device.terminal_sessions.append(identity)
        window.active_terminal_id = '0'
        def remove_page(index):
            terminal = pages[index]
            self.assertNotIn(terminal, window.terminals.values())
            self.assertTrue(terminal.closed)
            pages.pop(index)
        window.notebook = SimpleNamespace(page_num=lambda terminal: pages.index(terminal), remove_page=remove_page)
        return window, closed, removed

    def test_close_each_position_preserves_other_tabs_and_defers_widget_disposal(self):
        for count, index in [(1, 0), (2, 0), (2, 1), (3, 0), (3, 1), (3, 2)]:
            with self.subTest(count=count, index=index):
                window, closed, removed = self.surface(count)
                identities = set(window.terminals)
                identity = str(index)
                window.close_terminal(identity)
                window.close_terminal(identity)  # A second signal is harmless.
                self.assertTrue(window.runtimes[identity].closing)
                self.assertEqual(closed, [])  # Still inside simulated clicked signal.
                window.terminal_message(identity, {'action': 'invalid-stale-event'})
                window.terminal_exit(identity)
                drain()
                self.assertEqual(closed, [identity])
                self.assertEqual(removed, [identity])
                self.assertEqual(set(window.terminals), identities - {identity})
                self.assertFalse(window._shutting_down)
                self.assertTrue(window.runtimes[identity].closed)
                window.close_terminal(identity)
                self.assertEqual(closed, [identity])

    def test_active_process_requires_confirmation_before_quiescing(self):
        window, closed, _ = self.surface(2)
        window.terminals['1'].bridge.foreground_description = lambda: 'python3'
        requested = []
        window.confirm_action = lambda *args: requested.append(args)
        window.close_terminal('1')
        self.assertFalse(window.runtimes['1'].closing)
        self.assertEqual(closed, [])
        requested[0][-1]()
        drain()
        self.assertEqual(closed, ['1'])

    def test_close_twenty_tab_surfaces(self):
        for _ in range(20):
            window, closed, _ = self.surface(3)
            for identity in ('1', '0', '2'):
                window.close_terminal(identity)
                drain()
            self.assertEqual(closed, ['1', '0', '2'])
            self.assertFalse(window.terminals)


class PtyLifetimeTests(unittest.TestCase):
    def terminal(self):
        parser = OscParser(new_token(), lambda message: None)
        bridge = PtyBridge(['bash', '--noprofile', '--norc', '-i'], dict(os.environ), '/tmp', parser)
        pty = Vte.Pty.new_foreign_sync(bridge.take_front_master(), None)
        view = SimpleNamespace(pty=pty)
        view.set_pty = lambda value: self.fail('Close must not invoke the crashing public null setter')
        return SimpleNamespace(closed=False, state=TerminalLifecycle.RUNNING, _shutting_down=False,
            sources=Sources(), signals=Signals(), readers={}, writers={}, timer=None,
            parser=parser, bridge=bridge, view=view, pty=pty, buffers={},
            directory=tempfile.TemporaryDirectory(prefix='naui-tab-test-'), on_status=None, on_exit=None)

    def test_fifty_real_pty_closes_leave_other_shell_and_native_pty_alive(self):
        survivor = self.terminal()
        try:
            survivor_fd = survivor.pty.get_fd()
            def command_marker(marker):
                os.write(survivor.bridge.shell_master, ('printf "\\n%s\\n" ' + marker + '\n').encode())
                data, deadline = b'', time.monotonic() + 3
                expected = ('\r\n' + marker + '\r\n').encode()
                while expected not in data and time.monotonic() < deadline:
                    ready, _, _ = select.select([survivor.bridge.shell_master], [], [], .05)
                    if ready: data += os.read(survivor.bridge.shell_master, 8192)
                self.assertIn(expected, data)
            command_marker('SURVIVOR_BEFORE')
            for _ in range(50):
                terminal = self.terminal()
                fd = terminal.pty.get_fd()
                TerminalPane.close(terminal)
                TerminalPane.close(terminal)
                self.assertEqual(terminal.state, TerminalLifecycle.CLOSED)
                self.assertIsNotNone(terminal.bridge.process.returncode)
                self.assertFalse(Path(terminal.directory.name).exists())
                # The widget still owns its native PTY until normal EOF or
                # container disposal. Model that owner releasing its reference;
                # never call the public set_pty(None) to force its destruction.
                self.assertIsNone(terminal.pty)
                os.fstat(fd)
                terminal.view.pty = None
                gc.collect()
                with self.assertRaises(OSError): os.fstat(fd)
                os.fstat(survivor_fd)
                self.assertIsNone(survivor.bridge.process.poll())
            command_marker('SURVIVOR_AFTER')
        finally:
            TerminalPane.close(survivor)
            survivor.view.pty = None

    def test_protocol_callback_can_close_without_accessing_cleared_buffers(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'data')
            terminal = SimpleNamespace(_shutting_down=False, shell=read_fd, readers={read_fd: 1}, buffers={})
            def callback(data):
                terminal._shutting_down = True
                return data
            terminal.parser = SimpleNamespace(feed=callback)
            self.assertFalse(TerminalPane._read(terminal, read_fd, GLib.IO_IN))
        finally:
            os.close(read_fd); os.close(write_fd)


class SshOwnershipTests(unittest.TestCase):
    def test_transport_leases_close_independently_and_preserve_filesystem_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'file').write_text('remote text')
            backend = RemoteDeviceBackend('server')
            self.addCleanup(backend.close)
            backend.connect(RootedTransport('server', directory), 'one')
            first = backend._delegate
            generation = backend.generation
            backend.connect(RootedTransport('server', directory), 'two')
            second = backend._sessions['two']
            self.assertIs(backend._delegate, first)
            self.assertEqual(backend.generation, generation)
            self.assertTrue(first.connected)
            self.assertTrue(backend.release('one'))
            self.assertFalse(first.connected)
            self.assertTrue(second.connected)
            self.assertEqual(backend.read('/file').content, 'remote text')
            self.assertTrue(backend.release('one'))  # Duplicate notification.
            backend.connect(RootedTransport('server', directory), 'three')
            backend.select('three')
            self.assertTrue(backend.release('three'))  # Selected lease switches back.
            self.assertTrue(backend.has_session('two'))
            self.assertFalse(backend.release('two'))
            backend.close(); backend.close()

    def test_two_ssh_terminals_share_device_but_exit_only_ends_one(self):
        window = ControllerSurface()
        window.local('session-start', host='server', options=[], session='one')
        window.on_message({'action': 'session-ready', 'location': 'remote', 'session': 'one'})
        device = window.workspace.devices[window.runtime.device_id]
        self.addCleanup(device.filesystem_backend.close)
        original = window.runtime.id
        key = new_token()
        state = TerminalState('second', device.id, device.id, {None: dict(window.contexts[None])})
        window.runtimes[state.id] = state
        window.terminals[state.id] = SimpleNamespace(local_key=key, closed=False, view=SimpleNamespace(grab_focus=lambda: None))
        window.terminal_message(state.id, sign({'action': 'session-start', 'session': 'two', 'host': 'server', 'options': []}, key))
        window.terminal_message(state.id, {'action': 'session-ready', 'location': 'remote', 'session': 'two'})
        cancelled = []
        window.transfers = SimpleNamespace(cancel_device=cancelled.append)
        editor = SimpleNamespace(device_id=device.id, session='one',
            mark_disconnected=lambda *args: self.fail('Surviving SSH must retain document connection'))
        window.viewers.append(editor)
        window.disconnect_session('one')
        self.assertTrue(device.connected)
        self.assertEqual(editor.session, 'two')
        self.assertEqual(cancelled, [])
        self.assertFalse(window.runtimes[state.id].closed)
        self.assertFalse(window.session_is_current('one'))
        self.assertTrue(window.session_is_current('two'))
        self.assertEqual(len(window.workspace.devices), 2)
        self.assertEqual(window.session_terminals['one'], original)
        window.disconnect_session('one')
        self.assertTrue(device.connected)

    def test_parallel_pending_ssh_authentication_does_not_mark_device_connected(self):
        window = ControllerSurface()
        window.local('session-start', host='server', options=[], session='one')
        device = window.workspace.devices[window.runtime.device_id]
        self.addCleanup(device.filesystem_backend.close)
        first_transport = device.filesystem_backend._delegate
        key = new_token()
        state = TerminalState('pending-two', device.id, device.id, {None: dict(window.contexts[None])})
        window.runtimes[state.id] = state
        window.terminals[state.id] = SimpleNamespace(local_key=key, closed=False)
        window.terminal_message(state.id, sign({'action': 'session-start', 'session': 'two', 'host': 'server', 'options': []}, key))
        self.assertEqual(device.state, 'connecting')
        self.assertFalse(device.connected)
        self.assertIs(device.filesystem_backend._delegate, first_transport)
        window.on_message({'action': 'session-ready', 'location': 'remote', 'session': 'one'})
        self.assertTrue(device.connected)
        window.disconnect_session('two')
        self.assertTrue(device.connected)
        self.assertEqual(device.session_id, 'one')
        self.assertIs(device.filesystem_backend._delegate, first_transport)


class LayoutTests(unittest.TestCase):
    def test_layout_zoom_persist_and_reset_retains_font_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'ui.json')
            settings = UISettings(path)
            settings.save(400, sidebar_collapsed=True, transfer_height=210, window_width=900,
                          window_height=600, terminal_font=18, editor_font=16, source_control_expanded=False)
            restored = UISettings(path)
            self.assertEqual(restored.values, settings.values)
            restored.reset()
            self.assertEqual(UISettings(path).width, DEFAULTS['sidebar_width'])
            self.assertFalse(restored.values['sidebar_collapsed'])
            self.assertTrue(restored.values['source_control_expanded'])
            self.assertEqual(restored.values['editor_font'], 16)

    def test_invalid_sizes_types_and_existing_config_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'ui.json')
            settings = UISettings(path)
            settings.save(320, terminal_font=33)
            self.assertFalse(path.exists())
            settings.save(320, sidebar_collapsed=1)
            self.assertFalse(path.exists())
            path.write_text('{"sidebar_width": 320, "window_width": 100000}')
            settings = UISettings(path)
            self.assertFalse(settings.valid)
            settings.save(300)
            self.assertIn('100000', path.read_text())
            settings.reset()
            self.assertTrue(UISettings(path).valid)

    def test_legacy_sidebar_only_file_migrates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'ui.json')
            path.write_text('{"sidebar_width": 390}')
            settings = UISettings(path)
            self.assertEqual(settings.width, 390)
            self.assertEqual(settings.values['terminal_font'], 12)
            settings.save(editor_font=14)
            self.assertEqual(UISettings(path).values['editor_font'], 14)

class RemovalAndZoomTests(unittest.TestCase):
    def test_remove_workspace_device_preserves_remote_files_and_unsaved_document(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory, 'keep.txt'); file.write_text('remote contents')
            window = ControllerSurface()
            device = window.workspace.add_ssh('server')
            window.add_panel(device)
            device.filesystem_backend.connect(RootedTransport('server', directory), 'session')
            document = SimpleNamespace(device_id=device.id, backend=device.filesystem_backend,
                text='unsaved text', dirty=True, disconnected=False)
            document.mark_disconnected = lambda *args: setattr(document, 'disconnected', True)
            window.viewers.append(document)
            removed, watched, saved = [], [], []
            window.file_watch = SimpleNamespace(remove_device=watched.append)
            window.code_navigation = SimpleNamespace(remove_device=removed.append)
            window.workspace_sidebar.remove_device = lambda identity: window.workspace_sidebar.explorers.pop(identity)
            window.persist_workspace = lambda: saved.append(window.workspace.records())
            device.filesystem_backend.close()
            NauiWindow.finish_device_removal(window, device.id)
            self.assertNotIn(device.id, window.workspace.devices)
            self.assertIn('local', window.workspace_sidebar.explorers)
            self.assertEqual(file.read_text(), 'remote contents')
            self.assertEqual(document.text, 'unsaved text')
            self.assertTrue(document.disconnected)
            self.assertIs(document.backend, device.filesystem_backend)
            self.assertEqual(watched, [device.id])
            self.assertEqual(removed, [device.id])
            self.assertEqual([record['id'] for record in saved[-1]], ['local'])

    def test_removal_confirmation_names_device_and_never_removes_local(self):
        window = ControllerSurface()
        device = window.workspace.add_ssh('ubuntu@server', 'Staging')
        calls = []
        window.confirm_action = lambda *args: calls.append(args)
        window.remove_device = lambda identity: calls.append(identity)
        NauiWindow.remove_device_dialog(window, 'local')
        self.assertFalse(calls)
        NauiWindow.remove_device_dialog(window, device.id)
        self.assertIn('Staging', calls[0][1])
        self.assertIn('ubuntu@server', calls[0][1])
        self.assertIn('does not modify or shut down', calls[0][1])
        self.assertIn(device.id, window.workspace.devices)
        calls[0][-1]()
        self.assertEqual(calls[-1], device.id)

    def test_zoom_is_contextual_global_bounded_and_persisted(self):
        class Terminal:
            def __init__(self):
                self.closed = False
                self.fonts = []
                self.view = SimpleNamespace(set_font=lambda font: self.fonts.append(font.to_string()))
        class Editor: pass
        with tempfile.TemporaryDirectory() as directory:
            settings = UISettings(Path(directory, 'ui.json'))
            terminal, other = Terminal(), Terminal()
            page = [terminal]
            saves, editor_fonts = [], []
            window = SimpleNamespace(_shutting_down=False, ui_settings=settings,
                get_focus=lambda: None,
                notebook=SimpleNamespace(get_current_page=lambda: 0, get_nth_page=lambda index: page[0]),
                terminals={'one': terminal, 'two': other},
                save_ui=lambda: (settings.save(320), saves.append(True)),
                apply_editor_font=lambda: editor_fonts.append(settings.values['editor_font']))
            with patch('naui.gui.TerminalPane', Terminal), patch('naui.gui.EditorPane', Editor):
                NauiWindow.zoom_active(window, 1)
                self.assertEqual(terminal.fonts[-1], 'Monospace 13')
                self.assertEqual(other.fonts, terminal.fonts)
                page[0] = Editor()
                for _ in range(50): NauiWindow.zoom_active(window, -1)
                self.assertEqual(settings.values['editor_font'], 6)
                self.assertEqual(settings.values['terminal_font'], 13)
                NauiWindow.zoom_active(window, 0)
                self.assertEqual(settings.values['editor_font'], 12)
                self.assertEqual(UISettings(settings.path).values['terminal_font'], 13)
                page[0] = object()
                before = len(saves)
                NauiWindow.zoom_active(window, 1)
                self.assertEqual(len(saves), before)

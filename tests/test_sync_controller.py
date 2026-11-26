"""Exercise the actual OSC/PTY controller without requiring display access.

Only widget surfaces are stubbed. PTY/prompt and filesystem implementations are
exercised separately against real Bash/coreutils in the other integration tests.
"""
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from naui.backends import LocalFileBackend
from naui.navigation import NavigationHistory, PREPARE_NAVIGATION, cd_command
from naui.protocol import new_token, sign
from naui.workspace import Workspace, TerminalState

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Vte", "3.91")
    gi.require_version("GtkSource", "5")
    from naui.gui import NauiWindow
    from gi.repository import Gdk
except (ImportError, ValueError):
    NauiWindow = None


class ControllerSurface:
    def __init__(self):
        for name in ["on_message", "show_context", "cancel_navigation", "navigate_terminal", "disconnect_session",
                     "register_remote", "session_is_current", "in_terminal", "device_terminal", "activate_device",
                     "terminal_message", "update_workspace_ui", "terminal_exited", "terminal_exit", "page_switched", "bind_authentication"]:
            setattr(self, name, getattr(NauiWindow, name).__get__(self))
        from naui.authentication import AuthenticationManager
        self.authentication = AuthenticationManager()
        self.after_terminal_exit = lambda identity: None
        self.key = new_token()
        self.input, self.statuses, self.editors = [], [], []
        self.viewers = self.editors
        self.workspace = Workspace("/tmp")
        self.local_backend = self.workspace.devices["local"].filesystem_backend
        context = {"backend": self.local_backend, "path": "/tmp", "home": "/home/test", "state": "connected", "device_id": "local"}
        self.runtimes = {"terminal-local": TerminalState("terminal-local", "local", "local", {None: context})}
        self.workspace.devices["local"].terminal_sessions.append("terminal-local")
        self.active_terminal_id, self._event_terminal = "terminal-local", None
        self.session_devices, self.session_terminals, self.terminal_labels = {}, {}, {}
        self.sessions = {}
        self.alive = True
        self._shutting_down = False
        self.sources = SimpleNamespace(timeout=lambda milliseconds, callback, *args: 1, idle=lambda callback, *args: 1, remove=lambda source: None)
        self.set_status = self.statuses.append
        self.terminals = {"terminal-local": SimpleNamespace(local_key=self.key, closed=False, send_input=self.input.append,
                                          view=SimpleNamespace(grab_focus=lambda: None))}
        self.workspace_sidebar = SimpleNamespace(explorers={}, add_device=self.add_panel, update_devices=lambda: None)
        self.add_panel(self.workspace.devices["local"])
        self.device_indicator = SimpleNamespace(set_text=self.statuses.append)
        self.persist_workspace = lambda: None
        self.notebook = SimpleNamespace(page_num=lambda widget: 0, set_current_page=lambda index: None)

    def add_panel(self, device):
        explorer = SimpleNamespace(device=device, histories={device.id: device.navigation_state.history},
                                   session=device.session_id, path=device.navigation_state.current_path,
                                   tree_model=object(), inline=None,
                                   status=SimpleNamespace(set_text=self.statuses.append), update_controls=lambda: None)
        def context(backend, session, path, home, state=None):
            explorer.session, explorer.path = session, path
        explorer.set_context = context
        self.workspace_sidebar.explorers[device.id] = explorer

    def local(self, action, **fields):
        self.on_message(sign({"action": action, "location": "local", **fields}, self.key))


if NauiWindow is not None:
    for name in ("runtime", "terminal", "explorer", "contexts", "active_session", "at_prompt", "navigation_pending", "navigation_timeout"):
        setattr(ControllerSurface, name, getattr(NauiWindow, name))

@unittest.skipIf(NauiWindow is None, "GTK/VTE/Source GI bindings unavailable")
class SyncControllerTests(unittest.TestCase):
    def setUp(self):
        self.window = ControllerSurface()
        self.window.local("cwd", path="/tmp", home="/home/test")

    def test_cd_sent_only_after_signed_readline_acknowledgment(self):
        window = self.window
        target = "/tmp/space ';$(touch NAUI_INJECTED)"
        self.assertTrue(window.navigate_terminal(target, None))
        self.assertEqual(window.input, [PREPARE_NAVIGATION])
        window.local("cwd", path="/tmp", home="/home/test")
        self.assertEqual(window.input, [PREPARE_NAVIGATION])
        window.local("navigation-ready", path="/tmp", home="/home/test")
        self.assertEqual(window.input[-1], cd_command(target).encode())
        window.local("busy")
        self.assertIsNotNone(window.navigation_pending)
        window.local("cwd", path=target, home="/home/test")
        self.assertEqual(window.explorer.path, target)
        self.assertTrue(window.at_prompt)
        self.assertIsNone(window.navigation_pending)

    def test_foreground_and_continuation_prompts_block_navigation(self):
        window = self.window
        window.local("busy")
        self.assertFalse(window.navigate_terminal("/etc", None))
        self.assertFalse(window.input)
        window.local("cwd", path="/tmp", home="/home/test")
        window.navigate_terminal("/etc", None)
        window.local("busy")
        window.local("navigation-ready", path="/tmp", home="/home/test")
        self.assertEqual(window.input, [PREPARE_NAVIGATION])
        self.assertIsNone(window.navigation_pending)

    def test_media_tab_clipboard_shortcuts_do_not_paste_into_hidden_terminal(self):
        window = self.window
        pasted = []
        window.current_editor = lambda: None
        window.terminal.view.paste_clipboard = lambda: pasted.append(True)
        window.notebook.get_current_page = lambda: 0
        window.notebook.get_nth_page = lambda index: object()  # Active media tab.
        from naui.keybindings import CommandRegistry
        from naui.keybinding_host import register_core
        window.command_registry = CommandRegistry()
        register_core(window,window.command_registry)
        context = patch('naui.keybinding_host.focused_context',side_effect=lambda _: 'terminal' if window.notebook.get_nth_page(0) is window.terminal else 'global')
        context.start();self.addCleanup(context.stop)
        modifiers = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        self.assertFalse(NauiWindow.key_pressed(window, None, Gdk.KEY_v, 0, modifiers))
        self.assertFalse(pasted)
        window.notebook.get_nth_page = lambda index: window.terminal
        self.assertTrue(NauiWindow.key_pressed(window, None, Gdk.KEY_v, 0, modifiers))
        self.assertEqual(pasted, [True])

    def test_remote_context_and_history_cannot_switch_to_local_via_forged_output(self):
        window = self.window
        window.local("session-start", host="test-server", options=[], session="ssh-test")
        self.assertEqual(window.active_session, "ssh-test")
        window.on_message({"action": "cwd", "location": "remote", "session": "ssh-test",
                           "path": "/opt/project", "home": "/home/remote"})
        self.assertEqual(window.explorer.path, "/opt/project")
        window.on_message({"action": "cwd", "location": "local", "path": "/etc", "home": "/root"})
        self.assertEqual(window.explorer.path, "/opt/project")
        window.on_message({"action": "session-start", "host": "evil", "session": "evil", "options": []})
        self.assertNotIn("evil", window.sessions)
        window.local("session-end", session="ssh-test")
        self.assertEqual(window.active_session, None)
        self.assertEqual(window.explorer.path, "/tmp")
        remote = window.workspace.devices[window.session_devices["ssh-test"]]
        self.assertEqual(remote.navigation_state.history.current, "/opt/project")
        self.assertIn(remote.id, window.workspace_sidebar.explorers)
        self.assertIn("local", window.workspace_sidebar.explorers)
        self.assertFalse(window.at_prompt)
        window.local("cwd", path="/tmp", home="/home/test")
        self.assertTrue(window.at_prompt)

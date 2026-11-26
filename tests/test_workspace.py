"""Device ownership, exact remote Bash scripts and native headless tree models.

RootedTransport substitutes isolated temp directories for machines. This tests
backend/device routing, not real OpenSSH authentication or a displayed window.
"""
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from naui.backends import ConflictError, DisconnectedError
from naui.filesystem import FileEntry
from naui.workspace import (Device, RemoteDeviceBackend, TerminalState, Workspace, WorkspaceStore,
                            config_path, connection_metadata, root_path, ssh_config_hosts, ssh_host)
from naui.ssh import Connection
from naui.protocol import new_token, sign

try:
    from test_sync_controller import ControllerSurface, NauiWindow
    from naui.explorer import ExplorerSidebar
    from naui.gui import EditorPane
    from naui.lifecycle import FileWork, Signals, Sources
    from gi.repository import GLib, GtkSource
except (ImportError, ValueError):
    NauiWindow = None


class RootedTransport:
    """Run the real backend scripts with positional paths mapped to one machine."""
    username, options = None, ()
    def __init__(self, host, root):
        self.host, self.root = host, Path(root)

    def command(self, script, *, batch=False):
        arguments = shlex.split(script)
        assert arguments[:2] == ["bash", "-c"] and arguments[3] == "naui"
        return [*arguments[:4], *[str(self.root / arg.lstrip("/")) if arg.startswith("/") else arg
                                 for arg in arguments[4:]]]


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for workspace job")


class WorkspaceModelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = Workspace(str(self.root))

    def test_devices_roots_and_navigation_are_independent(self):
        local = self.workspace.devices["local"]
        a = self.workspace.add_ssh("user@example-server", "example-server")
        b = self.workspace.add_ssh("raspberrypi")
        local.observe(str(self.root / "src"))
        local.navigation_state.expanded.add(str(self.root / "src"))
        a.add_root("/opt/project")
        a.add_root("/var/log")
        a.observe("/opt/project/a", "/home/user")
        a.observe("/opt/project/b")
        self.workspace.active_device_id = a.id
        self.workspace.active_device_id = "local"
        self.assertEqual(local.navigation_state.current_path, str(self.root / "src"))
        self.assertIn(str(self.root / "src"), local.navigation_state.expanded)
        self.assertEqual(a.roots[0].navigation.history.current, "/opt/project/b")
        self.assertEqual(a.navigation_state.history.target(-1), "/opt/project/a")
        a.observe("/opt/project/a")
        self.assertEqual(a.navigation_state.history.target(1), "/opt/project/b")
        self.assertIsNone(b.navigation_state.current_path)
        self.assertEqual(len(self.workspace.devices), 3)

    def test_unpin_never_deletes_the_directory(self):
        local = self.workspace.devices["local"]
        local.remove_root(str(self.root))
        self.assertEqual(local.roots, [])
        self.assertTrue(self.root.is_dir())

    def test_reconnect_preserves_explicit_username_and_rejects_invalid_connection_metadata(self):
        device = self.workspace.add_ssh("user+team@server")
        device.username, device.port = connection_metadata(Connection(device.hostname, ("-l", "other", "-p", "2222")))
        self.assertEqual(device.reconnect_args(), ["-p", "2222", "-l", "other", "user+team@server"])
        self.assertEqual(Connection(device.hostname, tuple(device.reconnect_args()[:-1])).username, "other")
        store = WorkspaceStore(self.root / "workspace.json")
        store.save(self.workspace)
        restored = Workspace(str(self.root), store.load()).devices[device.id]
        self.assertEqual(restored.reconnect_args(), device.reconnect_args())
        for options in (("-p", "0"), ("-p", "65536"), ("-l", "user\ncontrol")):
            with self.assertRaises(ValueError):
                connection_metadata(Connection("server", options))

    def test_back_forward_survive_duplicate_ui_cwd_observations_and_are_root_scoped(self):
        device = self.workspace.devices["local"]
        a, b = str(self.root / "a"), str(self.root / "b")
        device.observe(a)
        device.observe(b)
        history = device.history_for_path(b)
        self.assertEqual(history.target(-1), a)
        device.observe(b)  # Selecting the terminal page before its cd completes.
        self.assertIsNotNone(history.pending_index)
        device.observe(a)
        self.assertEqual(history.target(1), b)
        other = str(self.root.with_name(self.root.name + "-other"))
        device.add_root(other)
        device.observe(other)
        self.assertIsNot(device.history_for_path(other), history)
        self.assertEqual(history.current, a)

    def test_persistence_only_contains_safe_metadata_and_loads_disconnected(self):
        device = self.workspace.add_ssh("ubuntu@server", "Staging")
        device.add_root("/opt/project ' õ")
        device.terminal_sessions.append("transient-terminal")
        device.session_id, device.password = "transient-session", "NOT_TO_BE_PERSISTED"
        store = WorkspaceStore(self.root / "config/naui/workspace.json")
        store.save(self.workspace)
        text = store.path.read_text()
        for value in ("NOT_TO_BE_PERSISTED", "transient-session", "transient-terminal", "ControlMaster", "password"):
            self.assertNotIn(value, text)
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        restored = Workspace(str(self.root), store.load())
        saved = restored.devices[device.id]
        self.assertEqual(saved.roots[0].path, "/opt/project ' õ")
        self.assertEqual(saved.display_name, "Staging")
        self.assertFalse(saved.connected)
        self.assertEqual(saved.terminal_sessions, [])
        self.assertEqual(saved.reconnect_args(), ["ubuntu@server"])

    def test_invalid_metadata_is_rejected_and_original_file_preserved(self):
        store = WorkspaceStore(self.root / "workspace.json")
        records = self.workspace.records()
        for data in (["wrong"], {"version": 1, "devices": records * 2},
                     {"version": 1, "devices": [{**records[0], "roots": ["relative"]}]},
                     {"version": 1, "devices": [{**records[0], "display_name": "name\ncontrol"}]}):
            original = json.dumps(data)
            store.path.write_text(original)
            with self.assertRaises(ValueError):
                store.load()
            self.assertEqual(store.path.read_text(), original)

    def test_config_uses_absolute_xdg_path_and_ignores_relative_setting(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}):
            self.assertEqual(config_path(), self.root / "naui/workspace.json")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}):
            self.assertEqual(config_path(), Path.home() / ".config/naui/workspace.json")

    def test_alias_suggestions_do_not_parse_keys_or_shell_options(self):
        config = self.root / "ssh_config"
        config.write_text('Host example-server raspberrypi\nHost *.example !excluded\nIdentityFile /DO_NOT_READ\nHost "quoted-alias"\n')
        self.assertEqual(ssh_config_hosts(config), ["example-server", "raspberrypi", "quoted-alias"])
        for bad in ("-oProxyCommand=touch", "host;touch", "host command", "host\n", "", "$(id)"):
            with self.assertRaises(ValueError):
                ssh_host(bad)
        self.assertEqual(ssh_host("user@[::1]"), "user@[::1]")
        with self.assertRaises(ValueError):
            root_path("relative")


class RemoteDeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.backends = []
        self.addCleanup(lambda: [backend.close() for backend in self.backends])

    def backend(self, name):
        directory = self.root / name / "project"
        directory.mkdir(parents=True)
        backend = RemoteDeviceBackend(name)
        backend.connect(RootedTransport(name, directory.parent))
        self.backends.append(backend)
        return backend, directory

    def test_identical_paths_save_to_the_correct_two_machines_and_survive_reconnect(self):
        a, root_a = self.backend("example-server")
        b, root_b = self.backend("raspberrypi")
        path = "/project/shared.txt"
        snap_a = a.write(path, "example-server", "missing")
        snap_b = b.write(path, "raspberrypi", "missing")
        a.close()
        with self.assertRaises(DisconnectedError):
            a.write(path, "preserved edit", snap_a.revision)
        a.connect(RootedTransport("example-server", root_a.parent))
        a.write(path, "saved after reconnect", snap_a.revision)
        b.write(path, "saved on pi", snap_b.revision)
        self.assertEqual((root_a / "shared.txt").read_text(), "saved after reconnect")
        self.assertEqual((root_b / "shared.txt").read_text(), "saved on pi")
        self.assertIsNot(a, b)

    def test_reconnect_keeps_revision_conflict_protection(self):
        backend, directory = self.backend("server")
        old = backend.write("/project/file", "old", "missing")
        backend.close()
        (directory / "file").write_text("external change")
        backend.connect(RootedTransport("server", directory.parent))
        with self.assertRaises(ConflictError):
            backend.write("/project/file", "unsaved edits", old.revision)
        self.assertEqual((directory / "file").read_text(), "external change")

    def test_backend_cannot_be_retargeted_and_close_is_idempotent(self):
        backend, directory = self.backend("server")
        with self.assertRaises(ValueError):
            backend.connect(RootedTransport("other-server", directory.parent))
        self.assertTrue(backend.connected)
        backend.close()
        generation = backend.generation
        backend.close()
        self.assertEqual(backend.generation, generation)


@unittest.skipIf(NauiWindow is None, "GI unavailable")
class DeviceControllerTests(unittest.TestCase):
    def setUp(self):
        self.window = ControllerSurface()
        self.window.local("cwd", path="/tmp", home="/home/test")

    def remote_terminal(self, host, session, path):
        window = self.window
        device = window.workspace.add_ssh(host)
        device.state = "connecting"
        window.add_panel(device)
        identity = "terminal-" + session
        runtime = TerminalState(identity, device.id, device.id, {None: {"backend": device.filesystem_backend,
                                "path": None, "home": None, "state": "connecting", "device_id": device.id}})
        window.runtimes[identity] = runtime
        device.terminal_sessions.append(identity)
        inputs, key = [], new_token()
        window.terminals[identity] = SimpleNamespace(local_key=key, closed=False, send_input=inputs.append,
                                                    view=SimpleNamespace(grab_focus=lambda: None))
        window.terminal_message(identity, sign({"action": "session-start", "session": session,
                                               "host": host, "options": []}, key))
        window.terminal_message(identity, {"action": "session-ready", "location": "remote", "session": session})
        window.terminal_message(identity, {"action": "cwd", "location": "remote", "session": session,
                                          "path": path, "home": "/home/user"})
        return device, runtime, inputs, key

    def test_background_events_and_device_switch_keep_local_tree_and_navigation(self):
        window = self.window
        tree = window.workspace_sidebar.explorers["local"].tree_model
        window.local("cwd", path="/tmp/local", home="/home/test")
        a, runtime, inputs, key = self.remote_terminal("example-server", "ssh-a", "/opt/project")
        b, other, _, _ = self.remote_terminal("raspberrypi", "ssh-b", "/home/pi")
        self.assertEqual(window.workspace.active_device_id, "local")
        self.assertEqual(window.contexts[None]["path"], "/tmp/local")
        window.activate_device(a.id)
        self.assertEqual(window.active_terminal_id, runtime.id)
        window.activate_device("local")
        self.assertIs(window.explorer.tree_model, tree)
        self.assertEqual(window.explorer.path, "/tmp/local")
        window.terminal_message(runtime.id, {"action": "cwd", "location": "remote", "session": "ssh-a",
                                             "path": "/opt/project/src", "home": "/home/user"})
        self.assertEqual(window.explorer.path, "/tmp/local")
        self.assertEqual(a.navigation_state.current_path, "/opt/project/src")
        self.assertEqual(b.navigation_state.current_path, "/home/pi")

    def test_foreign_terminal_cannot_open_or_update_another_devices_files(self):
        window = self.window
        a, origin, _, _ = self.remote_terminal("example-server", "ssh-a", "/project")
        b, other, _, _ = self.remote_terminal("raspberrypi", "ssh-b", "/project")
        opened = []
        window.open_file = lambda *arguments: opened.append(arguments)
        window.terminal_message(origin.id, {"action": "open", "location": "remote", "session": "ssh-b",
                                            "cwd": "/project", "args": ["secret"], "elevated": False})
        self.assertFalse(opened)
        window.terminal_message(origin.id, {"action": "cwd", "location": "remote", "session": "ssh-b",
                                            "path": "/wrong", "home": "/home/user"})
        self.assertEqual(b.navigation_state.current_path, "/project")

    def test_one_terminal_exiting_does_not_disconnect_other_devices(self):
        window = self.window
        a, origin, _, _ = self.remote_terminal("example-server", "ssh-a", "/project")
        b, other, _, _ = self.remote_terminal("raspberrypi", "ssh-b", "/project")
        window.terminal_exit(origin.id)
        self.assertEqual(a.state, "disconnected")
        self.assertTrue(b.connected)
        self.assertTrue(window.workspace.devices["local"].connected)
        self.assertFalse(window.runtimes["terminal-local"].closed)

    def test_navigation_timeout_is_bound_to_its_origin_after_switch(self):
        window = self.window
        a, origin, inputs, _ = self.remote_terminal("example-server", "ssh-a", "/project")
        calls = []
        window.sources.timeout = lambda delay, callback, *args: calls.append((callback, args)) or 1
        window.activate_device(a.id)
        window.navigate_terminal("/project/src", "ssh-a")
        window.activate_device("local")
        callback, arguments = calls[0]
        callback(*arguments)
        self.assertIsNone(origin.navigation_pending)
        self.assertTrue(window.at_prompt)
        self.assertEqual(window.workspace.active_device_id, "local")

    def test_late_old_ssh_exit_does_not_disconnect_reconnected_device_or_documents(self):
        window = self.window
        device, origin, _, old_key = self.remote_terminal("example-server", "ssh-old", "/project")
        backend = device.filesystem_backend
        document = SimpleNamespace(device_id=device.id, backend=backend, path="/project/config.json",
                                   session="ssh-old", disconnected=False, status=Label(), update_label=lambda: None)
        document.mark_disconnected = lambda *args: setattr(document, "disconnected", True)
        window.viewers.append(document)
        window.disconnect_session("ssh-old")
        self.assertTrue(document.disconnected)
        identity, key = "terminal-reconnect", new_token()
        device.state = "connecting"
        window.runtimes[identity] = TerminalState(identity, device.id, device.id,
            {None: {"backend": backend, "path": "/project", "home": "/home/user", "state": "connecting", "device_id": device.id}})
        device.terminal_sessions.append(identity)
        window.terminals[identity] = SimpleNamespace(local_key=key, closed=False, view=SimpleNamespace(grab_focus=lambda: None))
        window.terminal_message(identity, sign({"action": "session-start", "session": "ssh-new",
                                                "host": "example-server", "options": []}, key))
        window.terminal_message(identity, {"action": "session-ready", "location": "remote", "session": "ssh-new"})
        window.terminal_message(origin.id, sign({"action": "session-end", "session": "ssh-old"}, old_key))
        # Viewing an inactive terminal's scrollback cannot give it ownership
        # of a newer SSH transport or move that device's filesystem tree.
        window.page_switched(window.notebook, window.terminals[origin.id], 0)
        self.assertEqual(device.active_terminal_id, identity)
        generation = backend.generation
        window.terminal_exit(origin.id)
        window.terminal_exit(origin.id)  # Duplicate native notification is harmless.
        self.assertEqual(backend.generation, generation)
        self.assertTrue(device.connected)
        self.assertIs(document.backend, backend)
        self.assertEqual(document.session, "ssh-new")
        self.assertFalse(document.disconnected)
        self.assertEqual(window.workspace.devices["local"].navigation_state.current_path, "/tmp")

    def test_two_terminals_per_device_only_selected_terminal_moves_its_tree(self):
        window = self.window
        device = window.workspace.devices["local"]
        identity, key = "terminal-local-2", new_token()
        window.runtimes[identity] = TerminalState(identity, "local", "local",
            {None: {"backend": window.local_backend, "path": "/tmp/other", "home": "/home/test", "state": "connected", "device_id": "local"}})
        device.terminal_sessions.append(identity)
        terminal = SimpleNamespace(local_key=key, closed=False, view=SimpleNamespace(grab_focus=lambda: None))
        window.terminals[identity] = terminal
        window.terminal_message(identity, sign({"action": "cwd", "location": "local", "path": "/tmp/other", "home": "/home/test"}, key))
        self.assertEqual(window.explorer.path, "/tmp")
        window.page_switched(window.notebook, terminal, 0)
        self.assertEqual(window.explorer.path, "/tmp/other")
        window.page_switched(window.notebook, window.terminals["terminal-local"], 0)
        self.assertEqual(window.explorer.path, "/tmp")


class Label:
    def __init__(self): self.text = ""
    def set_text(self, text): self.text = text
    def get_text(self): return self.text
    def set_sensitive(self, value): pass


@unittest.skipIf(NauiWindow is None, "GI unavailable")
class NativeTreeMutationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = Workspace(str(self.root))
        self.sources, self.signals = Sources(), Signals()
        self.work = FileWork(self.sources)
        self.addCleanup(self.finish)
        self.surface = self.make_surface(self.workspace.devices["local"])
        self.surface.refresh()
        self.settle()

    def finish(self):
        self.sources.close()
        self.work.stop()
        self.work.join()
        self.signals.close()
        self.surface.list_view.set_model(None)
        self.surface.tree_model = self.surface.selection = None
        for device in self.workspace.devices.values():
            if device.type == "ssh":
                device.filesystem_backend.close()

    def make_surface(self, device):
        surface = SimpleNamespace(_shutting_down=False, device=device, backend=device.filesystem_backend,
                                  session=device.session_id, path=device.navigation_state.current_path,
                                  inline=None, inline_entry=None, pending_select=None, generation=0,
                                  tree_model=None, signals=self.signals, status=Label())
        surface.list_view = SimpleNamespace(set_model=lambda model: setattr(surface, "model", model))
        surface._close_popover = lambda: None
        surface._selection_changed = lambda: None
        surface.schedule_watches = lambda: None
        window = SimpleNamespace(_shutting_down=False, workspace=self.workspace, sources=self.sources,
                                 workspace_sidebar=SimpleNamespace(update_actions=lambda: None),
                                 work=self.work.submit, check_mutation=lambda *args, **kwargs: None,
                                 disconnect_session=lambda *args: None)
        surface.window = window
        for method in ("refresh", "_remember_expansion", "_restore_state", "_children", "_inline_node", "name_dialog",
                       "commit_inline", "cancel_inline", "error", "selected_parent", "selected_path", "reload_directory", "_confirmed_delete", "confirm_delete", "_after_operation"):
            setattr(surface, method, getattr(ExplorerSidebar, method).__get__(surface))
        return surface

    def settle(self):
        wait_for(lambda: not self.work._futures and not self.work._callbacks)

    def test_live_directory_reload_preserves_model_and_expanded_child_nodes(self):
        folder = self.root / 'expanded'
        folder.mkdir()
        (folder / 'child.txt').write_text('child')
        self.surface.refresh(); self.settle()
        row = next(self.surface.tree_model.get_row(index) for index in range(self.surface.tree_model.get_n_items())
                   if self.surface.tree_model.get_row(index).get_item().entry.path == str(folder))
        row.set_expanded(True); self.settle()
        node, model = row.get_item(), self.surface.tree_model
        (self.root / 'external.txt').write_text('external')
        self.surface.reload_directory(model.get_row(0).get_item()); self.settle()
        self.assertIs(self.surface.tree_model, model)
        new = next(model.get_row(index) for index in range(model.get_n_items())
                   if model.get_row(index).get_item().entry.path == str(folder))
        self.assertIs(new.get_item(), node)
        self.assertTrue(new.get_expanded())
        self.assertIn(str(folder / 'child.txt'), [entry.entry.path for entry in self.entries()])
        self.assertIn(str(self.root / 'external.txt'), [entry.entry.path for entry in self.entries()])

    def entries(self, surface=None):
        surface = surface or self.surface
        return [surface.tree_model.get_row(i).get_item() for i in range(surface.tree_model.get_n_items())]

    def mutate(self, action, name, entry=None, surface=None):
        surface = surface or self.surface
        surface.name_dialog(action, surface.path, surface.backend, surface.session, entry)
        self.settle()
        self.assertTrue(any(node.inline for node in self.entries(surface)))
        self.assertTrue(surface.commit_inline(name))
        self.settle()
        self.assertIsNone(surface.inline, surface.status.get_text())
        return next(node.entry for node in self.entries(surface) if node.entry.name == name)

    def test_inline_creation_escape_confirmation_selection_and_rename(self):
        surface = self.surface
        surface.name_dialog("New File", str(self.root), surface.backend, None)
        self.settle()
        self.assertTrue(any(node.inline for node in self.entries()))
        self.assertEqual(list(self.root.iterdir()), [])
        surface.cancel_inline()
        self.settle()
        self.assertEqual(list(self.root.iterdir()), [])
        file = self.mutate("New File", "file ';$(touch NEVER_EXECUTED) õ")
        self.assertEqual(surface.selection.get_selected_item().get_item().entry.path, file.path)
        directory = self.mutate("New Folder", "folder õ")
        self.assertTrue(Path(directory.path).is_dir())
        renamed_file = self.mutate("Rename", "renamed file õ", file)
        renamed_dir = self.mutate("Rename", "renamed folder õ", directory)
        self.assertFalse(Path(file.path).exists())
        self.assertFalse(Path(directory.path).exists())
        self.assertTrue(Path(renamed_file.path).is_file())
        self.assertTrue(Path(renamed_dir.path).is_dir())
        self.assertFalse(Path("NEVER_EXECUTED").exists())

    def test_conflict_and_permission_failure_keep_original_and_inline_input(self):
        (self.root / "original").write_text("keep")
        (self.root / "existing").write_text("existing")
        surface = self.surface
        original = surface.backend.properties(str(self.root / "original"))
        surface.name_dialog("Rename", str(self.root), surface.backend, None, original)
        self.settle()
        surface.commit_inline("existing")
        self.settle()
        self.assertIsNotNone(surface.inline)
        self.assertFalse(surface.inline["busy"])
        self.assertEqual((self.root / "original").read_text(), "keep")
        self.assertEqual((self.root / "existing").read_text(), "existing")
        surface.cancel_inline()
        self.settle()
        surface.name_dialog("New Folder", str(self.root), surface.backend, None)
        self.settle()
        with patch.object(surface.backend, "create", side_effect=PermissionError("Permission denied")):
            surface.commit_inline("cannot-create")
            self.settle()
        self.assertFalse((self.root / "cannot-create").exists())
        self.assertEqual(surface.inline["name"], "cannot-create")
        self.assertIn("Permission denied", surface.status.get_text())

    def test_delete_requires_confirmation_and_rechecks_editor_protection(self):
        file = self.mutate("New File", "config.json")
        confirmations = []
        self.surface.window.confirm_action = lambda *args: confirmations.append(args)
        self.surface.confirm_delete(file, self.surface.backend, None)
        self.assertTrue(Path(file.path).exists())
        self.assertIn(self.surface.device.display_name, confirmations[0][1])
        self.assertIn(file.path, confirmations[0][1])
        with patch.object(self.surface.window, "check_mutation", side_effect=ValueError("Editor opened meanwhile")):
            confirmations[0][3]()
            self.settle()
            self.assertTrue(Path(file.path).exists())
            self.assertIn("Editor opened meanwhile", self.surface.status.text)
        confirmations[0][3]()
        self.settle()
        self.assertFalse(Path(file.path).exists())

    def test_remote_inline_file_folder_rename_and_delete_use_device_backend(self):
        device = self.workspace.add_ssh("server")
        machine = self.root / "machine"
        (machine / "project").mkdir(parents=True)
        device.filesystem_backend.connect(RootedTransport("server", machine))
        device.state = "connected"
        device.observe("/project")
        device.add_root("/project")
        surface = self.make_surface(device)
        surface.refresh()
        self.settle()
        file = self.mutate("New File", "remote õ.txt", surface=surface)
        folder = self.mutate("New Folder", "remote folder", surface=surface)
        self.assertTrue((machine / "project/remote õ.txt").is_file())
        self.assertTrue((machine / "project/remote folder").is_dir())
        renamed = self.mutate("Rename", "renamed.txt", file, surface)
        renamed_folder = self.mutate("Rename", "renamed folder", folder, surface)
        self.assertFalse((machine / "project/remote folder").exists())
        self.assertTrue((machine / "project/renamed folder").is_dir())
        surface._confirmed_delete(renamed, surface.backend)
        surface._confirmed_delete(renamed_folder, surface.backend)
        self.settle()
        self.assertFalse((machine / "project/renamed.txt").exists())
        self.assertFalse((machine / "project/renamed folder").exists())
        surface.list_view.set_model(None)
        surface.tree_model = surface.selection = None

    def test_expansion_survives_refresh_and_other_device_models_remain_available(self):
        (self.root / "folder/child").mkdir(parents=True)
        self.surface.refresh()
        self.settle()
        row = next(self.surface.tree_model.get_row(index) for index in range(self.surface.tree_model.get_n_items())
                   if self.surface.tree_model.get_row(index).get_item().entry.name == "folder")
        row.set_expanded(True)
        self.settle()
        self.surface.refresh()
        self.settle()
        self.assertTrue(any(node.entry.name == "child" for node in self.entries()))
        self.assertIn(str(self.root / "folder"), self.surface.device.navigation_state.expanded)


class DocumentSurface:
    @property
    def dirty(self): return self.buffer.get_modified()


@unittest.skipIf(NauiWindow is None, "GI unavailable")
class EditorOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = Workspace(str(self.root))
        self.sources = Sources()
        self.work = FileWork(self.sources)
        self.addCleanup(self.finish)
        self.window = SimpleNamespace(_shutting_down=False, workspace=self.workspace, work=self.work.submit,
                                      disconnect_session=lambda *args: self.fail("A stale save must not disconnect a new transport"))
        from naui.ui_settings import DEFAULTS
        self.window.ui_settings = SimpleNamespace(values=dict(DEFAULTS))
        self.documents = []

    def finish(self):
        self.sources.close()
        self.work.stop()
        for device in self.workspace.devices.values():
            if device.type == "ssh":
                device.filesystem_backend.close()
        self.work.join()
        self.documents.clear()

    def document(self, device, path, content):
        backend = device.filesystem_backend
        snapshot = backend.read(path)
        document = DocumentSurface()
        document.window, document.backend, document.device_id = self.window, backend, device.id
        document.path, document.session, document.revision = path, device.session_id, snapshot.revision
        document._shutting_down = document.loading = document.saving = document.disconnected = False
        document.buffer = GtkSource.Buffer()
        document.buffer.set_text(content)
        from naui.editor_model import EditorDocument
        from naui.editor_view import GtkBufferAdapter
        document.document = EditorDocument(device.id,path,backend,GtkBufferAdapter(document.buffer),snapshot.revision)
        document.status = Label()
        document.update_label = lambda: None
        for name in ("save", "text", "failed", "mark_disconnected"):
            setattr(document, name, getattr(EditorPane, name).__get__(document))
        self.documents.append(document)
        return document

    def remote(self, name):
        device = self.workspace.add_ssh(name)
        root = self.root / name
        (root / "project").mkdir(parents=True)
        (root / "project/shared.txt").write_text("original")
        device.filesystem_backend.connect(RootedTransport(name, root))
        device.state = "connected"
        return device, root

    def test_ctrl_s_implementation_uses_document_backend_after_device_switch(self):
        a, root_a = self.remote("example-server")
        b, root_b = self.remote("raspberrypi")
        document_a = self.document(a, "/project/shared.txt", "edited on example-server")
        document_b = self.document(b, "/project/shared.txt", "edited on raspberrypi")
        local = self.workspace.devices["local"]
        local_file = self.root / "shared.txt"
        local_file.write_text("local original")
        document_local = self.document(local, str(local_file), "edited locally")
        self.workspace.active_device_id = b.id
        document_a.save()
        self.workspace.active_device_id = "local"
        document_b.save()
        self.workspace.active_device_id = a.id
        document_local.save()
        wait_for(lambda: not self.work._callbacks)
        self.assertEqual((root_a / "project/shared.txt").read_text(), "edited on example-server")
        self.assertEqual((root_b / "project/shared.txt").read_text(), "edited on raspberrypi")
        self.assertEqual(local_file.read_text(), "edited locally")
        self.assertFalse(any(document.dirty for document in self.documents))
        self.assertEqual(document_a.device_id, a.id)
        self.assertIs(document_a.backend, a.filesystem_backend)

    def test_opt_in_save_cleanup_is_undoable_and_uses_original_backend(self):
        from unittest.mock import Mock
        from naui.editor_view import EditorController
        local=self.workspace.devices['local']
        path=self.root/'cleanup.txt'
        path.write_bytes(b'one  \r\ntwo\t')
        document=self.document(local,str(path),'one  \r\ntwo\t')
        document.buffer.set_enable_undo(True)
        document.view=Mock()
        controller=document.editing=EditorController.__new__(EditorController)
        controller.pane,controller.closed=document,False
        self.window.ui_settings.values.update(editor_trim=True,editor_final_newline=True)
        document.save()
        wait_for(lambda:not self.work._callbacks)
        self.assertEqual(path.read_bytes(),b'one\r\ntwo\r\n')
        self.assertFalse(document.dirty)
        document.buffer.undo()
        self.assertEqual(document.text(),'one  \r\ntwo\t')
        self.assertTrue(document.dirty)

    def test_save_callback_from_old_connection_preserves_edits_without_closing_reconnect(self):
        device, root = self.remote("server")
        document = self.document(device, "/project/shared.txt", "my unsaved text")
        delegate = device.filesystem_backend._delegate
        entered, release = threading.Event(), threading.Event()
        write = delegate.write
        def delayed_write(*arguments):
            entered.set()
            release.wait(3)
            return write(*arguments)
        try:
            with patch.object(delegate, "write", side_effect=delayed_write):
                document.save()
                self.assertTrue(entered.wait(3))
                device.filesystem_backend.close()
                device.filesystem_backend.connect(RootedTransport("server", root))
                release.set()
                wait_for(lambda: not self.work._callbacks)
            self.assertTrue(document.dirty)
            self.assertFalse(document.disconnected)
            self.assertFalse(document.saving)
            self.assertTrue(device.filesystem_backend.connected)
            self.assertEqual((root / "project/shared.txt").read_text(), "original")
            self.assertIn("result unconfirmed", document.status.get_text())
        finally:
            release.set()

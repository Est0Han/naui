#!/usr/bin/env python3
"""Real GTK/VTE multi-device smoke test, using two disposable SSH simulations.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/workspace_smoke.py
No real SSH authentication/network acceptance is claimed. The test replaces the
ssh executable only inside its process PATH; production uses system OpenSSH.
"""
import gc
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time

import gi
for name, version in (("Gtk", "4.0"), ("Gdk", "4.0"), ("Vte", "3.91"), ("GtkSource", "5")):
    gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources
from naui.workspace import WorkspaceStore


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print("SKIP: no accessible display; native multi-device UI/shutdown not tested.")
        return 77
    with tempfile.TemporaryDirectory(prefix="naui-workspace-smoke-") as temporary:
        root = Path(temporary)
        local, a, b = root / "local", root / "test-a", root / "test-b"
        for directory in (local, a, b, root / "bin"):
            directory.mkdir()
        (local / "keep.py").write_text("# local original\n")
        os.environ.update(XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"))
        fake = root / "bin/ssh"
        fake.write_text(f"#!{sys.executable}\n" + '''import os,sys
from pathlib import Path
args=sys.argv[1:]
index=args.index('--')
host,command=args[index+1:index+3]
assert host in {'test-a','test-b'}, host
root=Path(__file__).resolve().parent.parent/host
os.environ['HOME']=str(root)
os.chdir(root)
os.execv('/bin/bash',['bash','-c',command])
''')
        fake.chmod(0o755)
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        app = NauiApplication([], str(local))
        sources = Sources()
        state = {"stage": "local-create", "phase": 0, "error": None, "closed": False}
        deadline = time.monotonic() + 90
        retained = []

        def inline(explorer, action, name, entry=None):
            explorer.name_dialog(action, explorer.path, explorer.backend, explorer.session, entry)
            state.update(action_name=name, explorer=explorer, phase=1)

        def tick():
            window = app.window
            try:
                assert time.monotonic() < deadline, "Workspace smoke timed out: " + state["stage"]
                if state["phase"] == 1:
                    explorer = state["explorer"]
                    if explorer.inline_entry is not None:
                        explorer.inline_entry.set_text(state["action_name"])
                        explorer.inline_entry.emit("activate")
                        state["phase"] = 2
                elif state["phase"] == 2:
                    explorer = state["explorer"]
                    if explorer.inline is None:
                        state["phase"] = 0
                        state["stage"] = {
                            "local-create": "local-folder", "local-folder": "local-rename-file",
                            "local-rename-file": "local-rename-folder", "local-rename-folder": "local-delete",
                            "remote-create": "remote-folder", "remote-folder": "documents",
                        }[state["stage"]]
                    elif not explorer.inline["busy"]:
                        raise AssertionError(explorer.status.get_text())
                elif state["stage"] == "local-create" and window.at_prompt:
                    inline(window.explorer, "New File", "new file õ.py")
                elif state["stage"] == "local-folder":
                    inline(window.explorer, "New Folder", "new folder õ")
                elif state["stage"] == "local-rename-file":
                    entry = window.local_backend.properties(str(local / "new file õ.py"))
                    inline(window.explorer, "Rename", "renamed file.py", entry)
                elif state["stage"] == "local-rename-folder":
                    entry = window.local_backend.properties(str(local / "new folder õ"))
                    inline(window.explorer, "Rename", "renamed folder", entry)
                elif state["stage"] == "local-delete":
                    entry = window.local_backend.properties(str(local / "renamed file.py"))
                    window.explorer.confirm_delete(entry, window.local_backend, None)
                    assert (local / "renamed file.py").exists()
                    assert str(local / "renamed file.py") in window.dialog.get_property("secondary-text")
                    window.dialog.emit("response", Gtk.ResponseType.ACCEPT)
                    state["stage"] = "add-device"
                elif state["stage"] == "add-device" and not (local / "renamed file.py").exists():
                    state["local_tree"] = window.workspace_sidebar.explorers["local"]
                    state["local_path"] = state["local_tree"].path
                    state["device_a"] = window.add_ssh_device("test-a", "Simulated example-server")
                    state["stage"] = "remote-wait"
                elif state["stage"] == "remote-wait" and window.at_prompt:
                    device = state["device_a"]
                    assert device.connected and len(window.workspace.devices) == 2
                    assert window.workspace_sidebar.explorers["local"] is state["local_tree"]
                    assert state["local_tree"].path == state["local_path"]
                    inline(window.explorer, "New File", "remote.txt")
                    state["stage"] = "remote-create"
                elif state["stage"] == "remote-folder":
                    inline(window.explorer, "New Folder", "remote folder õ")
                elif state["stage"] == "documents":
                    assert (a / "remote.txt").is_file() and (a / "remote folder õ").is_dir()
                    window.open_file(state["device_a"].filesystem_backend, str(a / "remote.txt"), state["device_a"].session_id)
                    window.activate_device("local")
                    window.terminal.send_input(b"echo hello\npwd\nnano keep.py\n")
                    state["stage"] = "save-documents"
                elif state["stage"] == "save-documents" and len(window.editors) == 2 and not any(e.loading for e in window.editors):
                    for editor in window.editors:
                        editor.buffer.set_text("local edit\n" if editor.device_id == "local" else "remote edit\n")
                        window.notebook.set_current_page(window.notebook.page_num(editor))
                        assert window.key_pressed(None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK)
                    state["stage"] = "disconnect"
                elif state["stage"] == "disconnect" and not any(e.saving for e in window.editors):
                    assert (local / "keep.py").read_text() == "local edit\n"
                    assert (a / "remote.txt").read_text() == "remote edit\n"
                    editor = next(e for e in window.editors if e.device_id == state["device_a"].id)
                    editor.buffer.set_text("remote edit after reconnect\n")
                    state["remote_editor"], state["remote_backend"] = editor, editor.backend
                    window.activate_device(state["device_a"].id)
                    window.terminal.send_input(b"exit\n")
                    state["stage"] = "reconnect"
                elif state["stage"] == "reconnect" and state["device_a"].state == "disconnected":
                    assert state["remote_editor"].dirty and state["remote_editor"].disconnected
                    window.activate_device("local")
                    assert window.explorer.path == state["local_path"]
                    window.activate_device(state["device_a"].id)
                    state["stage"] = "reconnected"
                elif state["stage"] == "reconnected" and state["device_a"].connected and window.at_prompt:
                    editor = state["remote_editor"]
                    assert not editor.disconnected and editor.backend is state["remote_backend"]
                    editor.save()
                    window.activate_device("local")
                    window.terminal.send_input(b"ssh test-b\n")
                    state["stage"] = "second-remote"
                elif state["stage"] == "second-remote" and len(window.workspace.devices) == 3 and window.at_prompt:
                    assert window.workspace.devices[window.runtime.device_id].hostname == "test-b"
                    assert state["device_a"].connected
                    window.activate_device("local")
                    state["stage"] = "local-restored"
                elif state["stage"] == "local-restored" and window.at_prompt and not any(e.saving for e in window.editors):
                    assert window.explorer.path == state["local_path"]
                    assert len(window.workspace_sidebar.explorers) == 3
                    assert (a / "remote.txt").read_text() == "remote edit after reconnect\n"
                    assert all(not e.dirty for e in window.editors)
                    sources.close()
                    window.close()
                    assert all(t.closed and t.bridge.process.returncode is not None and t.view.get_pty() is None
                               for t in window.terminals.values())
                    assert all(e.list_view.get_model() is None for e in window.workspace_sidebar.explorers.values())
                    state["closed"] = True
                    return
                sources.timeout(30, tick)
            except Exception as error:
                state["error"] = error
                sources.close()
                window._discard_approved = True
                window.close()

        def activated(application):
            retained.append(application.window)
            sources.timeout(30, tick)
        handler = app.connect_after("activate", activated)
        try:
            result = app.run([sys.argv[0]])
        finally:
            sources.close()
            app.disconnect(handler)
        if state["error"]:
            raise state["error"]
        assert state["closed"] and result == 0 and app.window is None
        records = WorkspaceStore(root / "config/naui/workspace.json").load()
        assert len(records) == 3
        window = retained.pop()
        assert all(not Path(t.directory.name).exists() for t in window.terminals.values())
        state.clear()
        del window, app
        gc.collect()
        print("PASS: GTK inline create/rename/delete, three device trees, two simulated SSH PTYs,")
        print("      nano, document save ownership, reconnect with unsaved edits, persistence and normal exit 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

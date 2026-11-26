#!/usr/bin/env python3
"""Opt-in real GTK/VTE acceptance test. Never writes to an existing user file.

Run on a desktop with all GI bindings: PYTHONPATH=src python3 tests/gui_smoke.py
It feeds a command through VTE's real PTY, waits for the OSC-opened tab, inserts
text, invokes the Ctrl+S handler, and verifies the saved file. No fake GTK objects.
"""
from pathlib import Path
import os
import shlex
import sys
import tempfile
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
gi.require_version("GtkSource", "5")
from gi.repository import Gdk, GLib, Gtk
from naui.gui import NauiApplication


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print("SKIP: GTK cannot access a graphical display from this session.")
        return 77
    with tempfile.TemporaryDirectory(prefix="naui-gui-test-") as root:
        os.environ["XDG_CONFIG_HOME"] = str(Path(root, "config"))
        path = Path(root, "naui-test.txt")
        app = NauiApplication([], root)
        deadline = time.monotonic() + 30
        state = {"stage": 0, "error": None}

        def tick():
            try:
                assert time.monotonic() < deadline, "Timed out waiting for GUI acceptance"
                window = app.window
                if window is None:
                    return True
                if state["stage"] == 0 and window.at_prompt:
                    window.terminal.view.feed_child(
                        ("echo hello\npwd\nnano " + shlex.quote(str(path)) + "\n").encode())
                    state["stage"] = 1
                elif state["stage"] == 1 and window.editors and not window.editors[0].loading:
                    editor = window.editors[0]
                    assert editor.revision == "missing", editor.status.get_text()
                    assert window.notebook.get_n_pages() == 2
                    editor.buffer.set_text("Hello from Naui\n")
                    assert editor.dirty
                    assert window.key_pressed(None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK)
                    state["stage"] = 2
                elif state["stage"] == 2 and not window.editors[0].saving:
                    editor = window.editors[0]
                    assert path.read_text() == "Hello from Naui\n", editor.status.get_text()
                    assert not editor.dirty
                    editor.show_search()
                    editor.search_entry.set_text("from")
                    editor._search_changed()
                    editor.find_next()
                    assert editor.buffer.get_selection_bounds()
                    # Terminal shortcuts retain shell semantics.
                    window.notebook.set_current_page(0)
                    assert not window.key_pressed(None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK)
                    window.terminal.send_input(b"mkdir -p naui-test/a/b\ncd naui-test\n")
                    state["stage"] = 3
                elif state["stage"] == 3 and window.explorer.path == str(Path(root, "naui-test")) and window.at_prompt:
                    model = window.explorer.tree_model
                    for index in range(model.get_n_items()):
                        row = model.get_row(index)
                        if row.get_item().entry.name == "a":
                            row.set_expanded(True)
                            state["stage"] = 4
                            break
                elif state["stage"] == 4:
                    assert window.explorer.path == str(Path(root, "naui-test")), "Expansion changed cwd"
                    model = window.explorer.tree_model
                    if any(model.get_item(index).get_item().entry.name == "b" for index in range(model.get_n_items())):
                        for index in range(model.get_n_items()):
                            if model.get_item(index).get_item().entry.name == "a":
                                window.explorer.list_view.emit("activate", index)
                                state["stage"] = 5
                                break
                elif state["stage"] == 5 and window.explorer.path == str(Path(root, "naui-test/a")) and window.at_prompt:
                    window.terminal.send_input(b"cd b\n")
                    state["stage"] = 6
                elif state["stage"] == 6 and window.explorer.path == str(Path(root, "naui-test/a/b")) and window.at_prompt:
                    window.explorer.path_entry.set_text("..")
                    window.explorer.path_entry.emit("activate")
                    state["stage"] = 7
                elif state["stage"] == 7 and window.explorer.path == str(Path(root, "naui-test/a")) and window.at_prompt:
                    window.explorer.history_move(-1)
                    state["stage"] = 8
                elif state["stage"] == 8 and window.explorer.path == str(Path(root, "naui-test/a/b")) and window.at_prompt:
                    window.explorer.history_move(1)
                    state["stage"] = 9
                elif state["stage"] == 9 and window.explorer.path == str(Path(root, "naui-test/a")) and window.at_prompt:
                    window.explorer.parent.emit("clicked")
                    state["stage"] = 10
                elif state["stage"] == 10 and window.explorer.path == str(Path(root, "naui-test")) and window.at_prompt:
                    window.explorer.refresh()
                    state["stage"] = 11
                elif state["stage"] == 11 and window.explorer.tree_model.get_n_items() > 1:
                    assert window.explorer.path == str(Path(root, "naui-test")), "Refresh changed cwd"
                    assert window.explorer.history.current == window.explorer.path
                    window.close()
                    print("PASS: real PTY/editor save/search; manual cd, tree expansion, double-click activation, path entry, Back/Forward, Parent and Refresh")
                    return False
            except Exception as exc:
                state["error"] = exc
                if app.window:
                    app.window._discard_approved = True
                    app.window.close()
                return False
            return True

        source = GLib.timeout_add(100, tick)
        app.run([sys.argv[0]])
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
        if state["error"]:
            raise state["error"]
        assert state["stage"] == 11, "Application exited before acceptance completed"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

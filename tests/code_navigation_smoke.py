#!/usr/bin/env python3
"""Desktop-only real GTK/Pyright navigation, rename/save and normal shutdown."""
import os
from pathlib import Path
import sys
import tempfile
import time

import gi
for name, version in (("Gtk", "4.0"), ("Gdk", "4.0"), ("Vte", "3.91"), ("GtkSource", "5")):
    gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print("SKIP: no accessible display; rendered code navigation not tested.")
        return 77
    if os.environ.get("NAUI_TEST_PYRIGHT_COMMAND"):
        os.environ["NAUI_PYRIGHT_COMMAND"] = os.environ["NAUI_TEST_PYRIGHT_COMMAND"]
    with tempfile.TemporaryDirectory(prefix="naui-code-smoke-") as directory:
        root = Path(directory)
        os.environ.update(XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"))
        main_path, helper_path = root / "main.py", root / "helper.py"
        main_path.write_text('from helper import greet\ngreet("hello")\nwrong: int = "text"\n')
        helper_path.write_text('def greet(name: str) -> str:\n    return name\n')
        app = NauiApplication([str(main_path)], str(root))
        sources = Sources()
        state = {"phase": "ready", "error": None}
        retained = []
        deadline = time.monotonic() + 60
        def tick():
            window = app.window
            try:
                assert time.monotonic() < deadline, "Code smoke timed out: " + state["phase"]
                if state["phase"] == "ready" and window.editors and not window.editors[0].loading:
                    editor = window.editors[0]
                    document = window.code_navigation.documents[editor]
                    if document.diagnostics:
                        state["main"] = editor
                        editor.buffer.place_cursor(editor.buffer.get_iter_at_offset(editor.text().index('greet("') + 2))
                        assert window.key_pressed(None, Gdk.KEY_F12, 0, 0)
                        state["phase"] = "definition"
                elif state["phase"] == "definition" and len(window.editors) == 2:
                    helper = next(editor for editor in window.editors if editor.path == str(helper_path))
                    if not helper.loading:
                        assert window.current_editor() is helper
                        assert helper.device_id == state["main"].device_id == "local"
                        assert helper.buffer.get_iter_at_mark(helper.buffer.get_insert()).get_line() == 0
                        state["helper"] = helper
                        window.code_navigation.history_move(-1)
                        assert window.current_editor() is state["main"]
                        window.code_navigation.query(state["main"], "references")
                        state["phase"] = "references"
                elif state["phase"] == "references" and state["main"].code_results_scroll.get_visible():
                    assert "references" in state["main"].code_status.get_text()
                    window.code_navigation.query(state["main"], "hover")
                    state["phase"] = "hover"
                elif state["phase"] == "hover" and "greet" in state["main"].code_status.get_text():
                    window.code_navigation.query(state["main"], "rename", new_name="welcome")
                    state["phase"] = "rename"
                elif state["phase"] == "rename" and window.dialog:
                    assert main_path.read_text().count("greet") == 2
                    assert helper_path.read_text().count("greet") == 1
                    assert "Change excerpt" in window.dialog.code_preview_text
                    window.dialog.emit("response", Gtk.ResponseType.ACCEPT)
                    assert all(editor.dirty for editor in window.editors)
                    assert all("welcome" in editor.text() for editor in window.editors)
                    for editor in window.editors:
                        window.notebook.set_current_page(window.notebook.page_num(editor))
                        assert window.key_pressed(None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK)
                    state["phase"] = "save"
                elif state["phase"] == "save" and not any(editor.saving for editor in window.editors):
                    assert all(not editor.dirty for editor in window.editors)
                    assert "welcome" in main_path.read_text() and "welcome" in helper_path.read_text()
                    state["sessions"] = list(window.code_navigation.sessions.values())
                    sources.close()
                    window.close()
                    state["phase"] = "closed"
                    return
                sources.timeout(30, tick)
            except Exception as error:
                state["error"] = error
                sources.close()
                window._discard_approved = True
                window.shutdown()
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
        assert state["phase"] == "closed" and result == 0 and app.window is None
        for session in state["sessions"]:
            assert session.client.process.returncode == 0
            assert all(not thread.is_alive() for thread in session.client._threads)
        assert retained[0].code_navigation.window is None
        print("PASS: GTK F12, references, hover, diagnostics, code history, reviewed rename/Ctrl+S and clean LSP/PTY shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

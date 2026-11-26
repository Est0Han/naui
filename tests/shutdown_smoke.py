#!/usr/bin/env python3
"""Desktop-only native GTK/VTE shutdown checks, in fresh interpreter processes.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings \
    python3 tests/shutdown_smoke.py --iterations 10

For gdb, select a case to debug that application's own process:
gdb --args python3 tests/shutdown_smoke.py --case tree
Optional real SSH: add --ssh-host YOUR_CONFIGURED_HOST (uses normal OpenSSH).
Exit 77 means the desktop is inaccessible, not a passing test.
"""
import argparse
import gc
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
gi.require_version("GtkSource", "5")
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources
from naui.viewers import ImageViewer, MediaViewer
from media_assets import make_assets

CASES = ("immediate", "commands", "tree", "editors", "shell-exit", "pending-files", "media", "workspace")


def run_case(case, ssh_host=None):
    with tempfile.TemporaryDirectory(prefix="naui-shutdown-test-") as root:
        root = Path(root)
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        (root / "a/b").mkdir(parents=True)
        (root / "first.py").write_text("print('hello')\n")
        (root / "second.conf").write_text("# hello\n")
        if case == "media":
            make_assets(root)
        app = NauiApplication([], str(root))
        sources = Sources()
        state = {"stage": 0, "error": None, "closed": False}
        deadline = time.monotonic() + (60 if case == "ssh" else 15)
        retained = []

        def close(window):
            sources.close()
            media = [viewer for viewer in window.viewers if isinstance(viewer, MediaViewer)]
            images = [viewer for viewer in window.viewers if isinstance(viewer, ImageViewer)]
            window.close()  # The same close-request/default handler as the title bar.
            assert window._shutting_down, "Normal close did not perform cleanup"
            assert window.terminal.bridge.process.returncode is not None, "Shell was not reaped"
            assert not window.sources._ids
            assert not window.file_work._callbacks
            assert not window.explorer.list_view.get_model()
            assert window.terminal.view.get_pty() is None
            assert all(viewer.media is None and viewer._shutting_down for viewer in media)
            assert all(viewer.canvas.texture is None and viewer._shutting_down for viewer in images)
            state["closed"] = True

        def tick():
            window = app.window
            try:
                assert time.monotonic() < deadline, f"Timeout in {case}, stage {state['stage']}"
                stage = state["stage"]
                if case == "commands":
                    if stage == 0 and window.at_prompt:
                        window.terminal.send_input(b"echo hello\npwd\ngit --version\ncd /tmp\n")
                        state["stage"] = 1
                    elif stage == 1 and window.at_prompt and window.explorer.path == "/tmp":
                        close(window)
                elif case == "tree":
                    if stage == 0 and window.at_prompt:
                        window.terminal.send_input(b"cd a\n")
                        state["stage"] = 1
                    elif stage == 1 and window.at_prompt and window.explorer.path == str(root / "a"):
                        model = window.explorer.tree_model
                        for index in range(model.get_n_items()):
                            row = model.get_row(index)
                            if row.get_item().entry.name == "b":
                                row.set_expanded(True)
                                assert window.explorer.path == str(root / "a")
                                window.explorer.list_view.emit("activate", index)
                                state["stage"] = 2
                                break
                    elif stage == 2 and window.at_prompt and window.explorer.path == str(root / "a/b"):
                        window.explorer.refresh()
                        close(window)  # Also closes during a freshly submitted listing.
                elif case == "editors":
                    if stage == 0 and window.at_prompt:
                        window.terminal.send_input(b"nano first.py second.conf\n")
                        state["stage"] = 1
                    elif stage == 1 and len(window.editors) == 2 and not any(e.loading for e in window.editors):
                        editor = window.editors[0]
                        editor.buffer.set_text("print('saved before closing')\n")
                        editor.save()
                        state["stage"] = 2
                    elif stage == 2 and not window.editors[0].saving:
                        assert not window.editors[0].dirty, window.editors[0].status.get_text()
                        assert (root / "first.py").read_text() == "print('saved before closing')\n"
                        for editor in list(window.editors):
                            window.close_editor(editor)
                        assert not window.editors
                        window.open_file(window.local_backend, str(root / "first.py"))
                        close(window)  # Close with a pending editor read.
                elif case == "shell-exit":
                    if stage == 0 and window.at_prompt:
                        window.terminal.send_input(b"exit\n")
                        state["stage"] = 1
                    elif stage == 1 and window.terminal.closed:
                        close(window)
                elif case == "media":
                    if stage == 0 and window.at_prompt:
                        window.open_path(window.local_backend, str(root / "pixels.png"))
                        window.open_path(window.local_backend, str(root / "tone.wav"))
                        state["stage"] = 1
                    elif stage == 1 and len(window.viewers) == 2 and not any(v.loading for v in window.viewers):
                        player = next(v for v in window.viewers if isinstance(v, MediaViewer))
                        assert player.media is not None
                        player.media.set_muted(True)
                        player.play()
                        # Close while the decoder is preparing/playing, with
                        # both native media/image tabs still owned by GTK.
                        close(window)
                elif case == "workspace":
                    if stage == 0 and window.at_prompt:
                        local = window.workspace.devices["local"]
                        window.create_terminal(local, str(root / "a"))
                        state["stage"] = 1
                    elif stage == 1 and window.at_prompt and window.explorer.path == str(root / "a"):
                        assert len(window.terminals) == 2
                        explorer = window.explorer
                        explorer.name_dialog("New File", str(root / "a"), explorer.backend, None)
                        state["stage"] = 2
                    elif stage == 2:
                        assert window.explorer.inline is not None
                        window.explorer.commit_inline("created-before-close.txt")
                        close(window)  # Close during a confirmed file creation.
                elif case == "ssh":
                    if stage == 0 and window.at_prompt:
                        window.terminal.send_input(("ssh " + shlex.quote(ssh_host) + "\n").encode())
                        state["stage"] = 1
                    elif stage == 1 and window.active_session and window.at_prompt:
                        window.terminal.send_input(b"cd /tmp\n")
                        state["stage"] = 2
                    elif stage == 2 and window.active_session and window.at_prompt and window.explorer.path == "/tmp":
                        assert window.explorer.session == window.active_session
                        window.terminal.send_input(b"exit\n")
                        state["stage"] = 3
                    elif stage == 3 and window.active_session is None and window.at_prompt:
                        close(window)
            except Exception as error:
                state["error"] = error
                # Test files only. Do not leave a failed smoke run's terminal
                # behind; the failure is re-raised after normal GTK shutdown.
                sources.close()
                window._discard_approved = True
                window.close()
            if not state["closed"] and state["error"] is None:
                sources.timeout(30, tick)

        def activated(application):
            window = application.window
            retained.append(window)
            try:
                if case == "immediate":
                    close(window)
                elif case == "pending-files":
                    window.open_file(window.local_backend, str(root / "first.py"))
                    window.work(lambda: time.sleep(0.15),
                                lambda *_: (_ for _ in ()).throw(AssertionError("Late UI callback")))
                    close(window)
                else:
                    sources.timeout(30, tick)
            except Exception as error:
                # PyGObject reports callback exceptions to stderr rather than
                # propagating them through Application.run; make the test fail.
                state["error"] = error
                sources.close()
                window._discard_approved = True
                if not window._shutting_down:
                    window.close()

        handler = app.connect_after("activate", activated)
        try:
            result = app.run([sys.argv[0]])
        finally:
            sources.close()
            app.disconnect(handler)
        if state["error"]:
            raise state["error"]
        assert state["closed"], "Window did not close"
        assert result == 0, f"Gtk.Application exited with {result}"
        assert app._shutting_down and app.window is None
        window = retained.pop()
        assert not Path(window.terminal.directory.name).exists(), "Runtime directory leaked"
        del window, app
        gc.collect()  # Exercise native disposal before interpreter shutdown too.
        print(f"PASS: {case}; exit 0")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--case", choices=(*CASES, "ssh"))
    parser.add_argument("--ssh-host")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    if args.case == "ssh" and not args.ssh_host:
        parser.error("the SSH case needs --ssh-host")
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print("SKIP: no accessible graphical display; shutdown was not tested.")
        return 77
    if args.case:
        return run_case(args.case, args.ssh_host)
    for iteration in range(args.iterations):
        case = CASES[iteration % len(CASES)]
        subprocess.run([sys.executable, __file__, "--case", case], check=True)
    if args.ssh_host:
        subprocess.run([sys.executable, __file__, "--case", "ssh", "--ssh-host", args.ssh_host], check=True)
    print(f"PASS: {args.iterations} fresh-process application open/close cycles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Desktop-only real GTK viewers/terminal/shutdown using generated fixtures.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/media_smoke.py
Codecs unavailable on the platform must produce a visible error, not a crash.
This validates playback advancement, not audible output through real speakers.
"""
import hashlib
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
from naui.backends import RemoteFileBackend
from naui.gui import NauiApplication
from naui.lifecycle import Sources
from naui.preview import PreviewCache
from naui.viewers import ImageViewer, MediaViewer
from media_assets import make_assets


class LocalPreviewTransport:
    host, username = "simulated-ssh", "test"
    def command(self, script, *, batch=False):
        return ["bash", "-c", script]


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print("SKIP: no accessible graphical display; native media/shutdown not tested.")
        return 77
    with tempfile.TemporaryDirectory(prefix="naui-media-test-") as temporary:
        os.environ["XDG_CONFIG_HOME"] = str(Path(temporary) / "config")
        root = make_assets(temporary, codecs=True)
        files = [root / name for name in ("pixels.png", "pixels.jpg", "tone.wav", "tone.mp3", "clip.mp4")
                 if (root / name).exists()]
        checksums = {path: hashlib.sha256(path.read_bytes()).digest() for path in files}
        remote = RemoteFileBackend(LocalPreviewTransport())
        app = NauiApplication([], str(root))
        sources = Sources()
        state = {"stage": "shell", "index": 0, "phase": 0, "error": None, "closed": False, "codec_skips": []}
        deadline = time.monotonic() + 60

        def tick():
            window = app.window
            try:
                assert time.monotonic() < deadline, f"Timed out in {state}"
                if state["stage"] == "shell" and window.at_prompt:
                    path = root / "from-nano.py"
                    window.terminal.send_input(("echo hello\npwd\ncd /tmp\nnano " + shlex.quote(str(path)) + "\n").encode())
                    state["stage"] = "nano"
                elif state["stage"] == "nano" and window.editors and not window.editors[0].loading:
                    editor = window.editors[0]
                    editor.buffer.set_text("print('Hello from Naui')\n")
                    window.key_pressed(None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK)
                    state["stage"] = "save"
                elif state["stage"] == "save" and not window.editors[0].saving and window.at_prompt:
                    assert (root / "from-nano.py").read_text() == "print('Hello from Naui')\n"
                    window.close_editor(window.editors[0])
                    window.explorer.path_entry.set_text(str(root))
                    window.explorer.path_entry.emit("activate")
                    state["stage"] = "navigate"
                elif state["stage"] == "navigate" and window.at_prompt and window.explorer.path == str(root):
                    assert window.explorer.tree_model is not None
                    state["stage"] = "previews"
                elif state["stage"] == "previews":
                    index, phase = state["index"], state["phase"]
                    if index > len(files):
                        window.show_about()
                        assert window.dialog.get_program_name() == "Naui"
                        window.dialog.close()
                        sources.close()
                        window.close()
                        state["closed"] = True
                        return
                    path = files[index] if index < len(files) else files[0]
                    if phase == 0:
                        # The final image uses the actual remote Bash/coreutils
                        # transfer implementation locally; no SSH auth claimed.
                        backend, session = (window.local_backend, None) if index < len(files) else (remote, "preview-test")
                        window.open_path(backend, str(path), session)
                        state["phase"] = 1
                    elif phase == 1 and window.viewers and not window.viewers[-1].loading:
                        viewer = window.viewers[-1]
                        if isinstance(viewer, ImageViewer):
                            assert viewer.canvas.texture is not None, viewer.status.get_text()
                            assert (viewer.original.get_width(), viewer.original.get_height()) == (16, 12)
                            viewer.set_zoom(1)
                            viewer.zoom_by(1.25)
                            assert viewer.canvas.zoom == 1.25
                            viewer.rotate(1)
                            assert viewer.canvas.texture.get_width() == 12
                            viewer.set_zoom(None)
                            if index == len(files):
                                assert viewer.source.cached and "simulated-ssh" in viewer.label.get_text()
                                cached = Path(viewer.source.local_path)
                                viewer.mark_disconnected()
                                assert "disconnected" in viewer.label.get_text()
                            else:
                                cached = None
                            window.close_viewer(viewer)
                            if cached:
                                assert not cached.exists()
                            state.update(index=index + 1, phase=0)
                        elif isinstance(viewer, MediaViewer):
                            assert viewer.media is not None, viewer.status.get_text()
                            if viewer.media.get_error():
                                assert "Playback unavailable" in viewer.status.get_text()
                                state["codec_skips"].append(path.name + ": " + viewer.status.get_text())
                                window.close_viewer(viewer)
                                state.update(index=index + 1, phase=0)
                            elif viewer.media.is_prepared():
                                viewer.media.set_muted(True)
                                viewer.play()
                                state["phase"] = 2
                    elif phase == 2:
                        viewer = window.viewers[-1]
                        if viewer.media.get_error():
                            state["codec_skips"].append(path.name + ": " + viewer.status.get_text())
                            window.close_viewer(viewer)
                            state.update(index=index + 1, phase=0)
                        elif viewer.media.get_timestamp() > 100_000 or viewer.media.get_ended():
                            viewer.pause()
                            viewer.volume.set_value(0.3)
                            assert abs(viewer.media.get_volume() - 0.3) < 0.001
                            if viewer.media.is_seekable() and viewer.media.get_duration() > 0:
                                viewer.seek.set_value(viewer.media.get_duration() / 2_000_000)
                            # Last player stays open during application shutdown
                            # in shutdown_smoke's media case as well.
                            window.close_viewer(viewer)
                            state.update(index=index + 1, phase=0)
                if not state["closed"]:
                    sources.timeout(30, tick)
            except Exception as error:
                state["error"] = error
                sources.close()
                window._discard_approved = True
                window.close()

        def activated(application):
            try:
                # Keep every test artifact beneath the disposable test directory.
                application.window.preview_cache.close()
                application.window.preview_cache = PreviewCache(root / "cache")
                application.window.sessions["preview-test"] = remote
                application.window.contexts["preview-test"] = {"backend": remote, "path": str(root),
                                                              "home": str(root), "state": "connected"}
                sources.timeout(30, tick)
            except Exception as error:
                state["error"] = error
                sources.close()
                application.window._discard_approved = True
                application.window.close()
        handler = app.connect_after("activate", activated)
        try:
            result = app.run([sys.argv[0]])
        finally:
            sources.close()
            app.disconnect(handler)
            remote.close()
        if state["error"]:
            raise state["error"]
        assert state["closed"] and result == 0 and app._shutting_down and app.window is None
        for path, checksum in checksums.items():
            assert hashlib.sha256(path.read_bytes()).digest() == checksum, "Preview modified source"
        assert not list((root / "cache").glob("preview-*"))
        for reason in state["codec_skips"]:
            print("CODEC UNAVAILABLE (visible error verified): " + reason)
        print("PASS: generated images/media, editor/nano/save, terminal/tree, simulated remote preview, About and normal exit 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

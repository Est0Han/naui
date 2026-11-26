"""Shutdown races using real GLib, native GObjects, PTYs and local processes.

These headless checks complement shutdown_smoke.py; they do not substitute for
closing a rendered GTK/VTE window under gdb on a desktop.
"""
import gc
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import weakref

from naui.backends import DisconnectedError, RemoteFileBackend
from naui.protocol import OscParser, new_token
from naui.pty_bridge import PtyBridge

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Vte", "3.91")
    gi.require_version("GtkSource", "5")
    from gi.repository import GLib, GtkSource, Vte
    from naui.gui import NauiWindow
    from naui.lifecycle import FileWork, Signals, Sources
    from naui.terminal import TerminalPane
    from naui.viewers import PreviewViewer, MediaViewer, ImageViewer
except (ImportError, ValueError):
    Sources = None


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for shutdown condition")


@unittest.skipIf(Sources is None, "GI bindings unavailable")
class MainLoopLifetimeTests(unittest.TestCase):
    def drain(self):
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def test_pending_sources_removed_and_late_registration_rejected(self):
        sources, called = Sources(), []
        idle = sources.idle(called.append, "idle")
        timer = sources.timeout(60000, called.append, "timer")
        sources.close()
        sources.close()
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(idle))
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(timer))
        self.assertIsNone(sources.idle(called.append, "late"))
        self.drain()
        self.assertEqual(called, [])

    def test_completion_runs_on_main_thread_and_sources_do_not_leak(self):
        sources = Sources()
        work = FileWork(sources)
        self.addCleanup(work.join)
        self.addCleanup(work.stop)
        self.addCleanup(sources.close)
        called = []
        main_thread = threading.get_ident()
        work.submit(lambda: (threading.get_ident(), "result"),
                    lambda value, error: called.append((threading.get_ident(), value, error)))
        wait_until(lambda: bool(work._results))
        self.drain()
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][0], main_thread)
        self.assertNotEqual(called[0][1][0], main_thread)
        self.assertEqual(called[0][1][1], "result")
        self.assertIsNone(called[0][2])
        self.assertFalse(sources._ids)
        self.assertFalse(work._callbacks)

    def test_native_buffer_retained_by_callback_is_released_on_main_thread(self):
        sources = Sources()
        work = FileWork(sources)
        entered, release = threading.Event(), threading.Event()
        disposed = []
        main_thread = threading.get_ident()
        buffer = GtkSource.Buffer()
        # GObject weak notifications observe actual native finalization.
        buffer.weak_ref(lambda: disposed.append(threading.get_ident()))
        callback = lambda value, error, owner=buffer: owner.set_text(value)
        owner = weakref.ref(buffer)

        def blocked_read():
            entered.set()
            release.wait(3)
            return "late result"

        try:
            work.submit(blocked_read, callback)
            self.assertTrue(entered.wait(3))
            del callback, buffer
            self.assertIsNotNone(owner())
            sources.close()
            work.stop()
            gc.collect()
            self.assertIsNone(owner())
            self.assertEqual(disposed, [main_thread])
            release.set()
            work.join()
            self.drain()
            self.assertFalse(work._results)
            self.assertFalse(sources._ids)
            self.assertIsNone(work.submit(lambda: "rejected", lambda *_: None))
        finally:
            release.set()
            sources.close()
            work.stop()
            work.join()

    def test_completion_queued_before_shutdown_is_cancelled(self):
        sources, called = Sources(), []
        work = FileWork(sources)
        try:
            work.submit(lambda: "result", lambda *args: called.append(args))
            wait_until(lambda: bool(work._results))
            self.assertTrue(sources._ids)
            sources.close()
            work.stop()
            work.stop()
            work.join()
            self.drain()
            self.assertEqual(called, [])
        finally:
            sources.close()
            work.stop()
            work.join()

    def test_signal_disconnection_is_idempotent(self):
        buffer, called = GtkSource.Buffer(), []
        signals = Signals()
        signals.connect(buffer, "modified-changed", lambda *_: called.append(True))
        signals.close()
        signals.close()
        buffer.set_text("No obsolete callback")
        self.assertEqual(called, [])


@unittest.skipIf(Sources is None, "GI bindings unavailable")
class WindowShutdownTests(unittest.TestCase):
    def test_player_detaches_and_clears_before_cache_release_and_ignores_late_events(self):
        events = []
        player = SimpleNamespace(
            pause=lambda: events.append("pause"),
            clear=lambda: events.append("clear"))
        viewer = SimpleNamespace(
            _shutting_down=False, media=player, window=object(), source=object(),
            signals=SimpleNamespace(close=lambda: events.append("disconnect")),
            video_widget=SimpleNamespace(set_media_stream=lambda stream: events.append(("detach", stream))),
            cache=SimpleNamespace(release=lambda source: events.append("release")))
        viewer.stop = lambda: MediaViewer.stop(viewer)
        PreviewViewer.close(viewer)
        PreviewViewer.close(viewer)
        self.assertEqual(events, ["disconnect", "pause", ("detach", None), "clear", "release"])
        self.assertIsNone(viewer.media)
        self.assertIsNone(viewer.window)
        # These callbacks must not touch any now-destroyed GTK surfaces.
        MediaViewer._updated(viewer)
        MediaViewer._seek_changed(viewer, None)
        MediaViewer._volume_changed(viewer, None)
        MediaViewer.play(viewer)
        MediaViewer.pause(viewer)
        PreviewViewer.update_label(viewer)
        PreviewViewer.mark_disconnected(viewer)
        ImageViewer.set_zoom(viewer, 1)
        ImageViewer.rotate(viewer, 1)
        self.assertEqual(len(events), 5)

    def test_cleanup_order_and_idempotence_without_manual_destroy(self):
        events = []
        def record(name):
            return lambda: events.append(name)
        window = SimpleNamespace(
            _shutting_down=False, alive=True, at_prompt=True, navigation_pending={"phase": "prepare"},
            workspace_dirty=False, runtimes={}, workspace=SimpleNamespace(devices={}),
            sources=SimpleNamespace(close=record("sources")),
            code_navigation=SimpleNamespace(close=record("stop-lsp"), cleanup=record("code-cache")),
            file_work=SimpleNamespace(stop=record("cancel-work"), join=record("join-work")),
            sessions={"ssh": SimpleNamespace(close=record("ssh"))},
            signals=SimpleNamespace(close=record("signals")),
            dialog=SimpleNamespace(destroy=record("dialog")),
            editors=[SimpleNamespace(close=record("editor"))],
            preview_cache=SimpleNamespace(cancel=record("cancel-previews"), close=record("cache")),
            pending_paths=set(),
            workspace_sidebar=SimpleNamespace(close=record("explorer")),
            left_sidebar=SimpleNamespace(close=record("activity-panels")),
            terminals={"local": SimpleNamespace(close=record("pty")), "remote": SimpleNamespace(close=record("remote-pty"))},
            destroy=lambda: self.fail("Shutdown must allow GTK's default close handler"))
        window.viewers = window.editors.copy()
        NauiWindow.shutdown(window)
        NauiWindow.shutdown(window)
        self.assertEqual(events, ["sources", "stop-lsp", "cancel-work", "cancel-previews", "ssh", "signals", "dialog", "editor",
                                  "explorer", "activity-panels", "pty", "remote-pty", "join-work", "code-cache", "cache"])
        self.assertTrue(window._shutting_down)
        self.assertFalse(window.alive)
        self.assertEqual(window.editors, [])

    def test_unsaved_close_cancel_and_keep_drafts_use_normal_close_path(self):
        callbacks, events = [], []
        editor = SimpleNamespace(saving=False, dirty=True)
        window = SimpleNamespace(_shutting_down=False, _discard_approved=False, editors=[editor],
                                 confirm_action=lambda title, message, label, callback: callbacks.append(callback),
                                 session_restore=SimpleNamespace(enabled=True, save=lambda: events.append("drafts")),
                                 shutdown=lambda: events.append("cleanup"), close=lambda: events.append("close"))
        self.assertTrue(NauiWindow.close_requested(window))
        self.assertEqual(events, [])  # Cancel: no changes discarded and no cleanup.
        callbacks[0]()
        self.assertEqual(events, ["drafts", "close"])
        self.assertFalse(NauiWindow.close_requested(window))
        self.assertEqual(events, ["drafts", "close", "cleanup"])

    def test_late_terminal_and_directory_controller_events_do_not_touch_widgets(self):
        window = SimpleNamespace(_shutting_down=True)
        NauiWindow.on_message(window, {"action": "session-end", "session": "gone"})
        NauiWindow.terminal_exited(window)
        NauiWindow.disconnect_session(window, "gone")
        self.assertFalse(NauiWindow.navigate_terminal(window, "/tmp", None))
        self.assertFalse(NauiWindow.set_status(window, "late"))
        self.assertFalse(NauiWindow.close_requested(window))

    def test_terminal_stops_and_reaps_child_without_public_vte_null_detach(self):
        events = []
        terminal = SimpleNamespace(
            closed=False, _shutting_down=False,
            sources=SimpleNamespace(close=lambda: events.append("sources")),
            readers={}, writers={}, timer=None,
            parser=SimpleNamespace(on_message=None, on_error=None), on_status=None, on_exit=None,
            bridge=SimpleNamespace(close=lambda: events.append("reap")),
            view=SimpleNamespace(set_pty=lambda pty: self.fail("Public VTE null detach caused the native crash")), pty=object(),
            buffers={}, directory=SimpleNamespace(cleanup=lambda: events.append("runtime")))
        TerminalPane.close(terminal)
        TerminalPane.close(terminal)
        self.assertEqual(events, ["sources", "reap", "runtime"])
        self.assertIsNone(terminal.pty)
        self.assertFalse(TerminalPane._read(terminal, -1, None))
        self.assertFalse(TerminalPane._write(terminal, -1, None))
        self.assertFalse(TerminalPane._tick(terminal))

    def test_natural_child_exit_removes_io_watch_before_direct_eof_drain(self):
        read_fd, write_fd = os.pipe()
        source = GLib.io_add_watch(read_fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                                   lambda *_: self.fail("Leaked IO watch"))
        events = []
        terminal = SimpleNamespace(
            _shutting_down=False, last_size=(24, 80), timer=999, readers={read_fd: source},
            view=SimpleNamespace(get_row_count=lambda: 24, get_column_count=lambda: 80),
            bridge=SimpleNamespace(process=SimpleNamespace(poll=lambda: 0, returncode=0)), shell=read_fd, front=write_fd,
            parser=SimpleNamespace(finish=lambda: b""), buffers={write_fd: bytearray()},
            _flush=lambda fd: None, close=lambda: events.append("close"), on_exit=lambda: events.append("exit"))
        def eof(fd, condition):
            self.assertIsNone(GLib.MainContext.default().find_source_by_id(source))
            terminal.readers.pop(fd, None)
            return False
        terminal._read = eof
        try:
            self.assertFalse(TerminalPane._tick(terminal))
            self.assertEqual(events, ["close", "exit"])
            self.assertIsNone(terminal.timer)
        finally:
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
            os.close(read_fd)
            os.close(write_fd)

    def test_foreign_vte_pty_owns_master_until_main_thread_release(self):
        bridge = PtyBridge(["bash", "--noprofile", "--norc", "-i"], dict(os.environ), "/tmp",
                           OscParser(new_token(), lambda message: None))
        fd = bridge.take_front_master()
        pty = Vte.Pty.new_foreign_sync(fd, None)
        disposed = []
        pty.weak_ref(lambda: disposed.append(threading.get_ident()))
        try:
            bridge.close()
            self.assertIsNotNone(bridge.process.returncode)
            os.fstat(fd)  # Bridge must never close VTE's descriptor behind it.
            del pty
            gc.collect()
            self.assertEqual(disposed, [threading.get_ident()])
            with self.assertRaises(OSError):
                os.fstat(fd)
        finally:
            bridge.close()


class ProcessShutdownTests(unittest.TestCase):
    def test_ten_immediate_real_pty_closes_are_reaped_and_idempotent(self):
        for iteration in range(10):
            with self.subTest(iteration=iteration):
                bridge = PtyBridge(["bash", "--noprofile", "--norc", "-i"], dict(os.environ), "/tmp",
                                   OscParser(new_token(), lambda message: None))
                started = time.monotonic()
                bridge.close()
                bridge.close()
                self.assertLess(time.monotonic() - started, 2)
                self.assertIsNotNone(bridge.process.returncode)
                self.assertEqual((bridge.front_master, bridge.front_slave, bridge.shell_master), (-1, -1, -1))
                with self.assertRaises(ChildProcessError):
                    os.waitpid(bridge.process.pid, os.WNOHANG)
        self.assertFalse(any(thread.name == "naui-shell-reaper" for thread in threading.enumerate()))

    def test_foreground_and_background_shell_jobs_stop(self):
        with tempfile.TemporaryDirectory() as root:
            foreground, background = Path(root, "foreground"), Path(root, "background")
            # exec preserves the foreground Bash PID so job control assigns a
            # process group distinct from the parent interactive shell.
            command = (f"sleep 1000 & echo $! > '{background}'; "
                       f"bash -c 'echo $$ > \"{foreground}\"; exec sleep 1000'\n")
            bridge = PtyBridge(["bash", "--noprofile", "--norc", "-i"], dict(os.environ), root,
                               OscParser(new_token(), lambda message: None))
            try:
                os.write(bridge.shell_master, command.encode())
                wait_until(lambda: foreground.exists() and background.exists())
                pids = [int(foreground.read_text()), int(background.read_text())]
                self.assertNotEqual(os.getpgid(pids[0]), bridge.process.pid)
                bridge.close()
                for pid in pids:
                    try:
                        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                    except FileNotFoundError:
                        continue
                    self.assertEqual(state, "Z", "A shell job survived shutdown")
            finally:
                bridge.close()

    def test_running_remote_transfer_is_cancelled_and_reaped(self):
        processes, errors = [], []
        class BlockingTransport:
            host = "fake-transport"
            def command(self, script, *, batch=False):
                return [sys.executable, "-c", "import time; time.sleep(1000)"]
        backend = RemoteFileBackend(BlockingTransport())
        def read():
            try:
                backend.read("/tmp/test")
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=read)
        thread.start()
        try:
            wait_until(lambda: bool(backend._processes))
            with backend._lock:
                processes.extend(backend._processes)
            backend.close()
            backend.close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(process.returncode is not None for process in processes))
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DisconnectedError)
            with self.assertRaises(DisconnectedError):
                backend.read("/tmp/new")
        finally:
            backend.close()
            thread.join(3)

    def test_ssh_wrapper_removes_runtime_socket_directory_on_sigterm(self):
        # Fake only the external SSH transport, using the REAL local wrapper
        # and TemporaryDirectory lifecycle. No server/password is needed.
        with tempfile.TemporaryDirectory() as root:
            runtime, tools = Path(root, "runtime"), Path(root, "bin")
            runtime.mkdir()
            tools.mkdir()
            marker = Path(root, "ssh-pid")
            fake = tools / "ssh"
            fake.write_text(f"#!{sys.executable}\nimport os,time\n"
                            f"open({str(marker)!r},'w').write(str(os.getpid()))\ntime.sleep(1000)\n")
            fake.chmod(0o755)
            env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), NAUI_TOKEN=new_token(), NAUI_LOCAL_KEY=new_token(),
                       PATH=str(tools) + os.pathsep + os.environ["PATH"],
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            helper = subprocess.Popen([sys.executable, "-m", "naui.shell", "ssh", "test-host"], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                wait_until(marker.exists)
                child_pid = int(marker.read_text())
                self.assertEqual(len(list(runtime.glob("naui-ssh-*"))), 1)
                helper.send_signal(signal.SIGTERM)
                helper.communicate(timeout=4)
                self.assertEqual(helper.returncode, 128 + signal.SIGTERM)
                self.assertFalse(list(runtime.iterdir()))
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if helper.poll() is None:
                    helper.kill()
                helper.communicate(timeout=3)

    def test_pty_hangup_cleans_ssh_wrapper_even_when_transport_ignores_term(self):
        from naui.integration import local_functions
        with tempfile.TemporaryDirectory() as root:
            runtime, tools = Path(root, "runtime"), Path(root, "bin")
            runtime.mkdir()
            tools.mkdir()
            marker = Path(root, "ssh-pid")
            fake = tools / "ssh"
            fake.write_text(f"#!{sys.executable}\nimport os,signal,time\n"
                            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                            "signal.signal(signal.SIGHUP,signal.SIG_IGN)\n"
                            f"open({str(marker)!r},'w').write(str(os.getpid()))\ntime.sleep(1000)\n")
            fake.chmod(0o755)
            rc = Path(root, "bashrc")
            rc.write_text(local_functions())
            env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), NAUI_TOKEN=new_token(), NAUI_LOCAL_KEY=new_token(),
                       PATH=str(tools) + os.pathsep + os.environ["PATH"],
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            bridge = PtyBridge(["bash", "--noprofile", "--rcfile", str(rc), "-i"], env, root,
                               OscParser(env["NAUI_TOKEN"], lambda message: None))
            try:
                os.write(bridge.shell_master, b"ssh test-host\n")
                wait_until(marker.exists)
                child_pid = int(marker.read_text())
                bridge.close()
                self.assertIsNotNone(bridge.process.returncode)
                self.assertFalse(list(runtime.iterdir()), "SSH socket directory survived PTY shutdown")
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                bridge.close()


if __name__ == "__main__":
    unittest.main()

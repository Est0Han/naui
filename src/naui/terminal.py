"""GLib-driven nonblocking PTY relay. Backpressure prevents output loss."""
import errno
import os
import pwd
import shlex
import tempfile
from pathlib import Path

from gi.repository import GLib, Gtk, Vte

from .integration import local_bash_rc, local_zsh_rc
from .protocol import OscParser, new_token
from .pty_bridge import PtyBridge
from .lifecycle import Sources, Signals
from .session_lifecycle import TerminalLifecycle


class TerminalPane(Gtk.Box):
    HIGH_WATER = 1024 * 1024

    def __init__(self, cwd, on_message, on_status, on_exit, *, command=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.token, self.local_key = new_token(), new_token()
        self.on_status, self.on_exit = on_status, on_exit
        self.closed = False
        self.state = TerminalLifecycle.RUNNING
        self.returncode = None
        self._shutting_down = False
        self.sources = Sources()
        self.signals = Signals()
        self.buffers, self.readers, self.writers = {}, {}, {}
        self.timer = None
        self.last_size = None
        self.directory = tempfile.TemporaryDirectory(prefix="naui-shell-", dir=os.environ.get("XDG_RUNTIME_DIR"))
        env = dict(os.environ)
        env.update(TERM="xterm-256color", COLORTERM="truecolor", NAUI_TOKEN=self.token,
                   NAUI_LOCAL_KEY=self.local_key)
        source = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
        shell = env.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
        name = Path(shell).name
        if command is not None:
            # A dedicated SSH terminal runs the existing LOCAL wrapper directly
            # on a real PTY; OpenSSH still owns every authentication prompt.
            command = list(command)
        elif name == "bash":
            rc = Path(self.directory.name) / "bashrc"
            rc.write_text(local_bash_rc(), encoding="utf-8")
            command = [shell, "--rcfile", str(rc), "-i"]
        elif name == "zsh":
            original = env.get("ZDOTDIR", env["HOME"])
            # Preserve the user's zshenv and zshrc without writing to their home.
            source_env = shlex.quote(str(Path(original, ".zshenv")))
            Path(self.directory.name, ".zshenv").write_text(
                f'if [ -r {source_env} ]; then source {source_env}; fi\n'
                + 'ZDOTDIR=' + shlex.quote(self.directory.name) + '\n', encoding="utf-8")
            Path(self.directory.name, ".zshrc").write_text(local_zsh_rc(), encoding="utf-8")
            env.update(ZDOTDIR=self.directory.name, NAUI_ORIGINAL_ZDOTDIR=original)
            command = [shell, "-i"]
        else:
            command = [shell, "-i"]
            self.sources.idle(on_status, "Your shell is running normally. nano/ssh integration requires Bash or zsh in this MVP.")
        self.view = Vte.Terminal()
        self.view.set_scrollback_lines(10000)
        self.view.set_hexpand(True)
        self.view.set_vexpand(True)
        self.view.set_audible_bell(False)
        self.append(self.view)
        self.parser = OscParser(self.token, on_message, lambda error: on_status("Rejected Naui request: " + error))
        self.bridge = PtyBridge(command, env, cwd, self.parser)
        # VtePty takes ownership of this master; it remains a real VTE terminal.
        self.pty = Vte.Pty.new_foreign_sync(self.bridge.take_front_master(), None)
        self.view.set_pty(self.pty)
        self.front, self.shell = self.bridge.front_slave, self.bridge.shell_master
        self.buffers = {self.front: bytearray(), self.shell: bytearray()}
        self.peers = {self.front: self.shell, self.shell: self.front}
        self._start_reader(self.front)
        self._start_reader(self.shell)
        self.timer = GLib.timeout_add(100, self._tick)

    def send_input(self, data):
        if self.closed:
            raise RuntimeError("The terminal shell has exited")
        self.view.feed_child(data)

    def _start_reader(self, fd):
        if fd not in self.readers and not self.closed:
            self.readers[fd] = GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT,
                                               GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read)

    def _read(self, fd, condition):
        if self._shutting_down:
            return False
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return True
        except OSError as exc:
            if exc.errno == errno.EIO:
                data = b""
            else:
                self.on_status(f"Terminal I/O error: {exc}")
                data = b""
        if not data:
            self.readers.pop(fd, None)
            return False
        if fd == self.shell:
            data = self.parser.feed(data)
        # Protocol delivery can synchronously initiate a terminal close. Do not
        # touch the relay's cleared buffers/fds after such a callback returns.
        if self._shutting_down:
            self.readers.pop(fd, None)
            return False
        destination = self.peers[fd]
        self.buffers[destination].extend(data)
        self._flush(destination)
        if len(self.buffers[destination]) >= self.HIGH_WATER:
            self.readers.pop(fd, None)
            return False
        return True

    def _flush(self, fd):
        if self._shutting_down:
            return
        queue = self.buffers[fd]
        try:
            while queue:
                count = os.write(fd, queue)
                del queue[:count]
        except BlockingIOError:
            if fd not in self.writers:
                self.writers[fd] = GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT,
                                                   GLib.IO_OUT | GLib.IO_ERR, self._write)
        except OSError as exc:
            self.on_status(f"Terminal I/O error: {exc}")
            queue.clear()
        if len(queue) < self.HIGH_WATER:
            self._start_reader(self.peers[fd])

    def _write(self, fd, condition):
        if self._shutting_down:
            return False
        self._flush(fd)
        if not self.buffers[fd]:
            self.writers.pop(fd, None)
            return False
        return True

    def _tick(self):
        if self._shutting_down:
            return False
        size = (self.view.get_row_count(), self.view.get_column_count())
        if size != self.last_size:
            self.bridge.resize(*size)
            self.last_size = size
        if self.bridge.process.poll() is not None:
            # Drain trailing output before closing the relay.
            # _read normally runs as an IO source: returning False removes it.
            # Here it is a DIRECT call, so remove its existing watch first.
            # Otherwise EOF pops the ID but leaves an untracked native source.
            source = self.readers.pop(self.shell, None)
            if source:
                GLib.source_remove(source)
            self._read(self.shell, GLib.IO_IN)
            self.buffers[self.front].extend(self.parser.finish())
            self._flush(self.front)
            self.timer = None
            self.returncode = self.bridge.process.returncode
            on_exit = self.on_exit
            self.close()
            on_exit()
            return False
        return True

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.state = TerminalLifecycle.CLOSING
        self._shutting_down = True
        self.sources.close()
        if hasattr(self, 'signals'):
            self.signals.close()
        for source in [*self.readers.values(), *self.writers.values(), self.timer]:
            if source:
                GLib.source_remove(source)
        self.readers.clear()
        self.writers.clear()
        self.timer = None
        # No application callbacks remain reachable through the parser while
        # shell/helper termination or VTE disposal produces final I/O events.
        self.parser.on_message = lambda message: None
        self.parser.on_error = lambda error: None
        self.on_status = self.on_exit = None
        self.bridge.close()
        # VTE owns the FRONT master only, not the Bash child on the second PTY.
        # It therefore must not watch_child()/reap the same subprocess.
        # DO NOT call view.set_pty(None): VTE 0.84.0's public setter dereferences
        # the null Pty in Widget::set_pty(), causing the observed SIGSEGV.
        # Closing the front slave above delivers EOF; VTE's internal EOF/dispose
        # paths release its own Pty safely. Drop only our Python reference here
        # and let normal notebook/container removal dispose the native widget.
        # No manual destroy/unparent, no duplicate fd close, and no app quit.
        self.pty = None
        self.buffers.clear()
        self.directory.cleanup()
        self.state = TerminalLifecycle.CLOSED

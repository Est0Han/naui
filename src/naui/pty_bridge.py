"""Two real PTYs, with a bounded protocol filter between shell and VTE.

VTE owns one master and handles input, terminal replies, paste, selection and
rendering normally. Its raw slave is bridged to the master of a second PTY.
The shell owns that second slave as its controlling terminal, including normal
job control, sudo/SSH prompts, termios and foreground process groups.

No manual command emulation and no ordinary-output pattern matching. This
module stays GTK-independent so the actual PTY bridge can be tested headlessly.
"""
import errno
import fcntl
import os
import signal
import struct
import subprocess
import termios
import tty
import time
from pathlib import Path


class PtyBridge:
    def __init__(self, command, env, cwd, parser):
        self.front_master, self.front_slave = os.openpty()
        tty.setraw(self.front_slave)
        self.shell_master, shell_slave = os.openpty()
        self.parser = parser
        self.process = None
        self.closed = False
        self.resize(24, 80)
        try:
            # Bash must receive a controlling tty. `start_new_session` alone
            # would not acquire an already-open PTY slave; the tiny Python
            # bootstrap below runs locally and execs the user's actual shell.
            bootstrap = (
                "import fcntl,os,sys,termios; "
                "os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); "
                "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
            )
            import sys
            self.process = subprocess.Popen([sys.executable, "-c", bootstrap, *command],
                                            stdin=shell_slave, stdout=shell_slave,
                                            stderr=shell_slave, env=env, cwd=cwd,
                                            close_fds=True)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(shell_slave)
        os.set_blocking(self.front_slave, False)
        os.set_blocking(self.shell_master, False)

    def take_front_master(self):
        fd, self.front_master = self.front_master, -1
        return fd

    def resize(self, rows, columns):
        if self.closed:
            return
        packed = struct.pack("HHHH", max(1, rows), max(1, columns), 0, 0)
        fcntl.ioctl(self.front_slave, termios.TIOCSWINSZ, packed)
        fcntl.ioctl(self.shell_master, termios.TIOCSWINSZ, packed)

    def stop_ssh_foreground(self, hostname):
        """Stop only our wrapped SSH foreground job; keep the outer shell/PTTY.

        Verify both the terminal-owned session and exact local wrapper argv.
        A delayed disconnect must never signal an unrelated new local program.
        """
        if self.closed or not self.process or self.process.poll() is not None:
            return False
        group = os.tcgetpgrp(self.shell_master)
        if group == self.process.pid:
            return False  # The outer shell already regained its foreground.
        if group not in self._session_groups():
            raise RuntimeError('SSH foreground no longer belongs to this terminal')
        with open(Path('/proc') / str(group) / 'cmdline', 'rb') as source:
            arguments = source.read(8192).split(b'\0')
        if arguments and arguments[-1] == b'':
            arguments.pop()
        marker = [b'-m', b'naui.shell', b'ssh']
        if not any(arguments[index:index + 3] == marker for index in range(len(arguments) - 2)) or arguments[-1] != hostname.encode():
            raise RuntimeError('SSH foreground changed; use exit in that terminal or explicitly close its tab')
        self._signal_group(group, signal.SIGTERM)
        deadline = time.monotonic() + 0.7
        while group in self._session_groups() and time.monotonic() < deadline:
            time.sleep(0.01)
        if group in self._session_groups():
            self._signal_group(group, signal.SIGKILL)
        return True

    def foreground_description(self):
        if self.closed or not self.process or self.process.poll() is not None:
            return None
        try:
            group = os.tcgetpgrp(self.shell_master)
            command = (Path('/proc') / str(group) / 'comm').read_text().strip()
            if group == self.process.pid and command in {'bash', 'zsh', 'fish', 'sh'}:
                return None
            return command or 'Foreground process'
        except (OSError, ValueError):
            return 'Foreground process'  # Fail conservatively before terminating.

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Bash job control gives foreground SSH/helpers a DIFFERENT process
        # group. killpg(shell.pid) alone misses them. Stop all groups in this
        # owned Linux session while the PTY and its widgets still exist.
        groups = self._session_groups()
        shell_group = self.process.pid if self.process else None
        for group in groups - {shell_group}:
            self._signal_group(group, signal.SIGTERM)
        if self.process and self.process.poll() is None:
            self._signal_group(shell_group, signal.SIGHUP)
            # Covers close immediately after Popen, before setsid/TIOCSCTTY.
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        # Hanging up the controlling PTY also signals privileged foreground
        # jobs (e.g. sudo), which our uid cannot signal directly. The VTE-facing
        # PTY and widget are still alive until termination/reaping completes.
        self._close_fd("shell_master")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not self._session_groups() and (not self.process or self.process.poll() is not None):
                break
            time.sleep(0.01)
        for group in self._session_groups():
            self._signal_group(group, signal.SIGKILL)
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()  # Reap before returning; no interpreter-time daemon thread.
        for name in ("front_master", "front_slave", "shell_master"):
            self._close_fd(name)
        self.parser = None

    def _session_groups(self):
        if self.process is None:
            return set()
        groups = set()
        # /proc is used only for our shell's unique session ID, never to kill
        # arbitrary children of other terminals. Zombie jobs are already dead.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()
                if int(fields[3]) == self.process.pid and fields[0] != "Z":
                    groups.add(int(fields[2]))
            except (OSError, ValueError, IndexError):
                continue
        return groups

    @staticmethod
    def _signal_group(group, signum):
        try:
            os.killpg(group, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # sudo descendants may belong to root; PTY hangup handles them.
            pass

    def _close_fd(self, name):
        fd = getattr(self, name)
        setattr(self, name, -1)
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

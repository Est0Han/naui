"""Optional OpenSSH inotify hints. No remote install or persistent resource.

The remote parent reads channel stdin. EOF/disconnect triggers its EXIT trap,
which terminates/reaps exactly its own inotifywait child, even when idle.
Names never enter the hint stream; backend snapshots supply normalized paths.
"""
import os
import select
import shlex
import subprocess

from .processes import stop_process
from .ssh import ssh_environment

WATCH_SCRIPT = r'''
command -v inotifywait >/dev/null || exit 69
[ -d "$1" ] && [ ! -L "$1" ] || exit 69
inotifywait -m -q -e create,delete,modify,attrib,moved_from,moved_to,delete_self,move_self --format 'naui-event' -- "$1" &
watcher=$!
cleanup() { kill "$watcher" 2>/dev/null || :; wait "$watcher" 2>/dev/null || :; }
trap cleanup EXIT
trap 'exit 0' HUP INT TERM
while IFS= read -r ignored; do :; done
'''


class RemoteWatchHint:
    def __init__(self, backend, path):
        self.backend, self.process = backend, None
        script = 'bash -c ' + shlex.quote(WATCH_SCRIPT) + ' naui ' + shlex.quote(path)
        with backend._lock:
            if backend._closed: raise OSError('Device disconnected')
            self.process = subprocess.Popen(backend.connection.command(script, batch=True),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=ssh_environment(), bufsize=0)
            backend._processes.add(self.process)
            backend._watch_hints.add(self)
        os.set_blocking(self.process.stdout.fileno(), False)

    def changed(self):
        if self.process is None: return False
        changed = False
        # Bounded work on the UI thread; no filename or shell-string parsing.
        for _ in range(4):
            ready, _, _ = select.select([self.process.stdout], [], [], 0)
            if not ready: break
            try: data = os.read(self.process.stdout.fileno(), 4096)
            except BlockingIOError: break
            if not data:
                self.close()
                break
            changed = True
        return changed

    def close(self):
        process, self.process = self.process, None
        if process is None: return
        process.stdin.close()  # Give remote cleanup EOF before ending local ssh.
        try: process.wait(timeout=.2)
        except subprocess.TimeoutExpired: stop_process(process, grace=.2)
        process.stdout.close()
        with self.backend._lock:
            self.backend._processes.discard(process)
            self.backend._watch_hints.discard(self)

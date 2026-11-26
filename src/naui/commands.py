"""Core device command transport. Plugins submit argv, never SSH shell strings."""
import os
import select
import subprocess
import time
import threading
import tempfile

from .backends import FileError
from .processes import stop_process

MAX_COMMAND_OUTPUT = 8 * 1024 * 1024
REMOTE_COMMAND = r'''
set -e
export GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0
cd -- "$1"
shift
command -v -- "$1" >/dev/null || { printf '%s not found on this device\n' "$1" >&2; exit 127; }
exec "$@"
'''


def validate_command(argv, cwd):
    if (not isinstance(argv, (list, tuple)) or not argv or len(argv) > 256
            or any(not isinstance(arg, str) or '\0' in arg or len(arg.encode()) > 16384 for arg in argv)
            or sum(len(arg.encode()) for arg in argv) > 65536
            or not argv[0] or not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd
            or len(cwd.encode()) > 4096):
        raise ValueError('Expected bounded command argv and an absolute working directory')
    return tuple(argv)


def local_command(argv, cwd, *, timeout=20, limit=MAX_COMMAND_OUTPUT, cancelled=None, input_data=None):
    argv = validate_command(argv, cwd)
    if input_data is not None and (not isinstance(input_data, bytes) or len(input_data) > MAX_COMMAND_OUTPUT):
        raise ValueError('Command input exceeds 8 MiB')
    input_file = tempfile.TemporaryFile() if input_data is not None else None
    if input_file:
        input_file.write(input_data); input_file.seek(0)
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=input_file if input_file else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    except FileNotFoundError as exc:
        if input_file: input_file.close()
        raise FileError(argv[0] + ' not found on this device (or working directory missing)') from exc
    except OSError:
        if input_file: input_file.close()
        raise
    # Drain both pipes incrementally: malicious Git output cannot allocate an
    # unbounded buffer or deadlock stderr while stdout is being consumed.
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    try:
        pending = set(buffers)
        while pending:
            if cancelled and cancelled.is_set():
                raise FileError('Device command cancelled')
            if time.monotonic() > deadline:
                raise FileError('Device command timed out')
            ready, _, _ = select.select(list(pending), [], [], .1)
            for stream in ready:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    pending.remove(stream)
                    continue
                buffers[stream].extend(chunk)
                if sum(map(len, buffers.values())) > limit:
                    raise FileError('Device command output exceeds limit')
        process.wait(timeout=max(.1, deadline - time.monotonic()))
        if process.returncode:
            raise FileError(bytes(buffers[process.stderr][:8192]).decode('utf-8', 'replace').strip()
                            or f'Device command failed ({process.returncode})')
        return bytes(buffers[process.stdout])
    finally:
        stop_process(process)
        process.stdout.close(); process.stderr.close()
        if input_file: input_file.close()


class CommandService:
    def __init__(self):
        self.cancelled = threading.Event()

    def execute(self, backend, argv, cwd, *, input_data=None):
        validate_command(argv, cwd)
        if self.cancelled.is_set():
            raise FileError('Device command service stopped')
        if hasattr(backend, 'connection'):
            return backend.run_command(argv, cwd) if input_data is None else backend.run_command(argv, cwd, input_data=input_data)
        return local_command(argv, cwd, cancelled=self.cancelled, input_data=input_data)

    def close(self):
        self.cancelled.set()

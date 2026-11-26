"""Core binary streams: bounded I/O and explicit atomic publication.

Remote scripts use only existing Bash/coreutils via the user's OpenSSH. They
never install a helper. A destination cannot publish on EOF: after the exact
byte count, the sender must explicitly authorize COMMIT. Cancellation closes
stdin, leaving the old destination intact and allowing the EXIT trap to clean.
"""
import os
from pathlib import Path
import select
import shlex
import stat
import subprocess
import tempfile
import threading
import time

from .backends import ConflictError, FileError, DisconnectedError
from .filesystem import entry_revision, local_entry, parse_entries, REMOTE_LIST
from .processes import stop_process
from .ssh import ssh_environment

CHUNK_SIZE = 65536
REMOTE_READ_BINARY = r'''
set -e
export LC_ALL=C
p=$1
[ ! -L "$p" ] && [ -f "$p" ] || { echo 'Only regular non-symlink files can transfer' >&2; exit 1; }
[ "$(stat -c '%d:%i:%f:%s:%y:%z' -- "$p")" = "$2" ] || exit 73
head -c "$3" -- "$p"
[ "$(stat -c '%d:%i:%f:%s:%y:%z' -- "$p")" = "$2" ] || exit 73
'''
REMOTE_WRITE_BINARY = r'''
set -e
export LC_ALL=C
umask 077
p=$1; expected=$2; bytes=$3; mode=$4
dir=${p%/*}; [ -n "$dir" ] || dir=/
check() {
    if [ "$expected" = missing ]; then
        [ ! -e "$p" ] && [ ! -L "$p" ]
    else
        [ ! -L "$p" ] && [ -f "$p" ] && [ "$(stat -c '%d:%i:%f:%s:%y:%z' -- "$p")" = "$expected" ]
    fi
}
check || exit 73
tmp=$(mktemp -- "$dir/.naui-partial-XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
head -c "$bytes" > "$tmp"
[ "$(stat -c %s -- "$tmp")" = "$bytes" ] || { echo 'Incomplete transfer; destination unchanged' >&2; exit 1; }
IFS= read -r ack
[ "$ack" = NAUI_COMMIT ] || exit 1
check || exit 73
if [ "$expected" != missing ]; then
    chown --reference="$p" -- "$tmp"
    chmod --reference="$p" -- "$tmp"
else
    chmod -- "$mode" "$tmp"
fi
sync -f -- "$tmp"
check || exit 73
if [ "$expected" = missing ]; then
    ln -- "$tmp" "$p" || exit 73
    rm -f -- "$tmp"
else
    mv -fT -- "$tmp" "$p"
fi
'''


class LocalReader:
    def __init__(self, path, info, control):
        self.path, self.info, self.control = path, info, control
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        self.file = os.fdopen(fd, 'rb', buffering=0)
        try:
            self.validate()
        except BaseException:
            self.file.close()
            raise

    def read(self, size):
        self.control.check()
        return self.file.read(size)

    def validate(self):
        info = os.fstat(self.file.fileno())
        if not stat.S_ISREG(info.st_mode) or entry_revision(info) != self.info.revision:
            raise ConflictError('Source changed during transfer')

    def finish(self):
        self.validate()

    def close(self):
        self.file.close()


class LocalWriter:
    def __init__(self, path, info, expected, control):
        self.path, self.info, self.expected, self.control = path, info, expected, control
        fd, self.temporary = tempfile.mkstemp(prefix='.naui-partial-', dir=os.path.dirname(path))
        self.file = os.fdopen(fd, 'wb', buffering=0)
        self.written = 0

    def write(self, data):
        self.control.check()
        view = memoryview(data)
        while view:
            self.control.check()
            count = self.file.write(view)
            if not count:
                raise FileError('Destination stopped accepting data')
            view = view[count:]
        self.written += len(data)

    def commit(self):
        self.control.check()
        if self.written != self.info.size:
            raise FileError('Incomplete transfer; destination unchanged')
        try:
            current = local_entry(self.path)
        except FileNotFoundError:
            current = None
        if (current.revision if current else 'missing') != self.expected:
            raise ConflictError('Destination changed during transfer')
        mode = self.info.mode & 0o777
        if current:
            if current.kind != 'f':
                raise ConflictError('Destination is no longer a regular file')
            original = os.lstat(self.path)
            temp_info = os.fstat(self.file.fileno())
            if (original.st_uid, original.st_gid) != (temp_info.st_uid, temp_info.st_gid):
                os.fchown(self.file.fileno(), original.st_uid, original.st_gid)
            mode = current.mode
        os.fchmod(self.file.fileno(), mode)
        os.fsync(self.file.fileno())
        self.control.check()
        if self.expected == 'missing':
            os.link(self.temporary, self.path)  # Atomic no-overwrite publication.
            os.unlink(self.temporary)
        else:
            if local_entry(self.path).revision != self.expected:
                raise ConflictError('Destination changed before publication')
            os.replace(self.temporary, self.path)
        with_directory = os.open(os.path.dirname(self.path), os.O_DIRECTORY)
        try:
            os.fsync(with_directory)
        finally:
            os.close(with_directory)

    def close(self):
        self.file.close()
        Path(self.temporary).unlink(missing_ok=True)


class RemoteStream:
    def __init__(self, backend, code, args, control, *, writing=False, control_input=False):
        self.backend, self.control, self.writing = backend, control, writing
        self.closed = False
        self.errors = bytearray()
        self.error_thread = None
        self.auth = None
        try:
            if getattr(backend, 'authentication', None):
                self.auth = backend.authentication.operation(backend.device_id, control)
                environment = self.auth.__enter__()
            else:
                environment = ssh_environment()
            script = 'bash -c ' + shlex.quote(code) + ' naui ' + ' '.join(shlex.quote(str(x)) for x in args)
            with backend._lock:
                if backend._closed or not backend.connected:
                    raise DisconnectedError('Transfer device disconnected')
                self.process = subprocess.Popen(backend.connection.command(script, batch=self.auth is None),
                    stdin=subprocess.PIPE if writing or control_input else subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL if writing else subprocess.PIPE,
                    stderr=subprocess.PIPE, env=environment, bufsize=0)
                backend._processes.add(self.process)
            self.error_thread = threading.Thread(target=self._drain_errors, name='naui-transfer-stderr', daemon=True)
            self.error_thread.start()
            self.pipe = self.process.stdin if writing else self.process.stdout
            os.set_blocking(self.pipe.fileno(), False)
        except BaseException:
            self.close()
            raise

    def _drain_errors(self):
        # Drain with a bounded tail instead of growing a local stderr tempfile
        # during very large/vvv SSH transfers. No GTK or unbounded log storage.
        try:
            while True:
                data = self.process.stderr.read(4096)
                if not data:
                    return
                self.errors.extend(data)
                del self.errors[:-8192]
        except (OSError, ValueError):
            pass

    def _ready(self, writing=False):
        deadline = time.monotonic() + 180
        while True:
            deadline += self.control.check() or 0
            if self.backend._closed:
                raise DisconnectedError('Transfer device disconnected')
            ready = select.select([] if writing else [self.pipe], [self.pipe] if writing else [], [], 0.1)
            if ready[0] or ready[1]:
                return
            if time.monotonic() > deadline:
                raise FileError('Transfer stream timed out')

    def read(self, size):
        self._ready()
        return os.read(self.pipe.fileno(), size)

    def write(self, data):
        view = memoryview(data)
        while view:
            self._ready(True)
            count = os.write(self.pipe.fileno(), view)
            view = view[count:]

    def finish(self):
        deadline = time.monotonic() + 30
        while self.process.poll() is None:
            deadline += self.control.check() or 0
            if time.monotonic() > deadline:
                raise FileError('Transfer completion timed out')
            time.sleep(0.01)
        if self.error_thread:
            self.error_thread.join(timeout=1)
        message = bytes(self.errors).decode('utf-8', 'replace').strip()
        if self.process.returncode == 73:
            raise ConflictError('Source or destination changed during transfer')
        if self.process.returncode:
            raise FileError(message or f'OpenSSH transfer failed ({self.process.returncode})')

    def commit(self):
        self.control.check()
        self.write(b'NAUI_COMMIT\n')
        self.pipe.close()
        self.finish()

    def close(self):
        if self.closed:
            return
        self.closed = True
        process = getattr(self, 'process', None)
        if process:
            if not self.writing and process.stdin:
                # Optional EOF control channel for owned remote search jobs.
                process.stdin.close()
            if self.writing and process.stdin:
                process.stdin.close()  # EOF triggers remote partial-file cleanup.
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
            stop_process(process, grace=0.3)
            if not self.writing and process.stdout:
                process.stdout.close()
            if self.error_thread:
                self.error_thread.join(timeout=1)
            process.stderr.close()
            with self.backend._lock:
                self.backend._processes.discard(process)
        if self.auth:
            self.auth.__exit__(None, None, None)
        self.errors.clear()


def remote_reader(backend, path, info, control):
    return RemoteStream(backend, REMOTE_READ_BINARY, [path, info.revision, info.size], control)


def remote_writer(backend, path, info, expected, control):
    return RemoteStream(backend, REMOTE_WRITE_BINARY, [path, expected, info.size, format(info.mode & 0o777, 'o')], control, writing=True)


def remote_entries(backend, path, control, expected=None):
    check = """[ ! -L "$1" ] || exit 1
[ "$(stat -c '%d:%i:%f:%s:%y:%z' -- "$1")" = "$2" ] || exit 73
""" if expected else '[ ! -L "$1" ] || exit 1\n'
    code = 'set -e\nexport LC_ALL=C\n' + check + REMOTE_LIST + ('\n' + check if expected else '')
    stream = RemoteStream(backend, code, [path, expected.revision] if expected else [path], control)
    pending, fields = bytearray(), []
    try:
        while True:
            data = stream.read(CHUNK_SIZE)
            if not data:
                break
            pending.extend(data)
            while b'\0' in pending:
                end = pending.index(0)
                fields.append(bytes(pending[:end]))
                del pending[:end + 1]
                if len(fields) == 7:
                    yield parse_entries(b'\0'.join(fields) + b'\0', path)[0]
                    fields.clear()
            if len(pending) > 16384:
                raise FileError('Invalid remote transfer listing')
        if pending or fields:
            raise FileError('Incomplete remote transfer listing')
        stream.finish()
    finally:
        stream.close()


def available_destination(backend, path):
    """Backend-level Keep Both naming. Publication remains atomic/no-overwrite."""
    parent, name = os.path.split(path)
    stem, extension = os.path.splitext(name)
    if not stem:
        stem, extension = name, ''
    for number in range(1, 10001):
        candidate = os.path.join(parent, f'{stem} ({number}){extension}')
        if backend.transfer_properties(candidate) is None:
            return candidate
    raise FileError('No available Keep Both name after 10000 attempts')

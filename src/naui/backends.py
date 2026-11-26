"""UTF-8 file backends with optimistic conflict checks and atomic replacement."""
import hashlib
import os
from pathlib import Path
import shlex
import stat as stat_module
import subprocess
import threading
import tempfile
import select
import time
from contextlib import nullcontext
from dataclasses import dataclass

from .ssh import Connection, ssh_environment
from .transfer_context import CURRENT_TRANSFER
from .processes import stop_process
from .preview import FileProbe
from .viewer_registry import PROBE_BYTES
from .filesystem import (FileEntry, MAX_ENTRIES, REMOTE_CREATE, REMOTE_LIST, REMOTE_MUTATE,
                         REMOTE_PROPERTIES, entry_revision, local_entry,
                         parse_entries, rename_no_replace)

MAX_FILE_SIZE = 8 * 1024 * 1024


class FileError(OSError):
    pass


class ConflictError(FileError):
    pass


class DisconnectedError(FileError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    content: str
    revision: str  # "missing" is an explicit expected state, never unconditional overwrite
    mode: int = 0o600


def _decode(data):
    if len(data) > MAX_FILE_SIZE:
        raise FileError("File exceeds the 8 MiB MVP editing limit")
    if b"\0" in data:
        raise FileError("Binary files cannot be edited as UTF-8 text")
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise FileError("File is not valid UTF-8") from exc


def _encode(content):
    data = content.encode("utf-8")
    _decode(data)
    return data


class LocalFileBackend:
    def run_command(self, argv, cwd):
        from .commands import local_command
        return local_command(argv, cwd)

    def transfer_properties(self, path):
        try:
            return self.properties(path)
        except FileNotFoundError:
            return None

    def available_destination(self, path):
        from .transfer_io import available_destination
        return available_destination(self, path)

    def iter_directory(self, path, control, expected=None):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if expected and entry_revision(os.fstat(descriptor)) != expected.revision:
                raise ConflictError('Source directory changed before enumeration')
            with os.scandir(descriptor) as directory:
                for item in directory:
                    control.check()
                    item.name.encode('utf-8')
                    info = os.stat(item.name, dir_fd=descriptor, follow_symlinks=False)
                    kind = ('d' if stat_module.S_ISDIR(info.st_mode) else 'f' if stat_module.S_ISREG(info.st_mode)
                            else 'l' if stat_module.S_ISLNK(info.st_mode) else 'o')
                    yield FileEntry(item.name, os.path.join(path, item.name), kind, info.st_size,
                        stat_module.S_IMODE(info.st_mode), int(info.st_mtime),
                        f'{info.st_uid}:{info.st_gid}', entry_revision(info))
            if expected and entry_revision(os.fstat(descriptor)) != expected.revision:
                raise ConflictError('Source directory changed during enumeration')
        finally:
            os.close(descriptor)

    def transfer_reader(self, path, info, control):
        from .transfer_io import LocalReader
        return LocalReader(path, info, control)

    def transfer_writer(self, path, info, expected, control):
        from .transfer_io import LocalWriter
        return LocalWriter(path, info, expected, control)

    def probe(self, path):
        path = os.path.realpath(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            if not Path(path).parent.is_dir():
                raise FileError("Parent directory does not exist")
            return FileProbe(path, b"", False)
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FileError("Only regular files can be viewed")
        with os.fdopen(fd, "rb") as source:
            return FileProbe(path, source.read(PROBE_BYTES))

    def copy_to(self, path, destination, limit):
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FileError("Only regular files can be previewed")
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat_module.S_ISREG(before.st_mode) or before.st_size > limit:
                raise FileError("Preview requires a regular file within the download limit")
            with open(destination, "wb") as target:
                remaining = limit
                while True:
                    data = source.read(min(65536, remaining + 1))
                    if not data:
                        break
                    remaining -= len(data)
                    if remaining < 0:
                        raise FileError("Preview exceeds the download limit")
                    target.write(data)
            if self._metadata(before) != self._metadata(os.fstat(source.fileno())):
                raise ConflictError("File changed while copying the preview")

    def list_directory(self, path):
        entries = []
        with os.scandir(path) as directory:
            for item in directory:
                if len(entries) >= MAX_ENTRIES:
                    raise FileError("Directory exceeds the 10000-entry limit")
                try:
                    entries.append(local_entry(item.path, item.name))
                except FileNotFoundError:
                    continue  # A concurrent removal does not invalidate the listing.
                except UnicodeError as exc:
                    raise FileError("Directory contains a filename that is not valid UTF-8") from exc
        return sorted(entries, key=lambda entry: (not entry.is_directory, entry.name.casefold(), entry.name))

    def properties(self, path):
        return local_entry(path)

    def create(self, path, directory=False):
        if directory:
            os.mkdir(path, 0o700)
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)

    def rename(self, path, destination, expected):
        self._check_entry(path, expected)
        rename_no_replace(path, destination)

    def delete(self, path, expected):
        info = self._check_entry(path, expected)
        if stat_module.S_ISDIR(info.st_mode):
            os.rmdir(path)  # Deliberately refuse recursive deletion.
        else:
            os.unlink(path)

    @staticmethod
    def _check_entry(path, expected):
        info = os.lstat(path)
        if entry_revision(info) != expected:
            raise ConflictError("Selection changed; refresh before retrying")
        return info

    def read(self, path):
        path = os.path.realpath(path)
        try:
            # A selected FIFO/device must not hang a worker while opening. A
            # nonblocking fd also handles replacement races before fstat.
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat_module.S_ISREG(before.st_mode):
                    raise FileError("Only regular files can be edited")
                data = source.read(MAX_FILE_SIZE + 1)
                after = os.fstat(source.fileno())
            if self._metadata(before) != self._metadata(after):
                raise ConflictError("File changed while being read; reopen it")
            content = _decode(data)
            revision = self._revision(after, data)
            return FileSnapshot(path, content, revision, stat_module.S_IMODE(after.st_mode))
        except FileNotFoundError:
            if not Path(path).parent.is_dir():
                raise FileError("Parent directory does not exist")
            return FileSnapshot(path, "", "missing")

    @staticmethod
    def _metadata(info):
        return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_mode}"

    def _revision(self, info, data):
        return self._metadata(info) + ":" + hashlib.sha256(data).hexdigest()

    def stat(self, path):
        return self.read(path)

    def exists(self, path):
        return self.read(path).revision != "missing"

    def write(self, path, content, expected):
        data = _encode(content)
        current = self.read(path)
        if current.revision != expected:
            raise ConflictError("File changed on disk. Your edits are preserved; reopen or save a copy manually.")
        destination = current.path
        fd, temporary = tempfile.mkstemp(prefix=".naui-", dir=str(Path(destination).parent))
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                if current.revision != "missing":
                    info = os.stat(destination)
                    temp_info = os.fstat(target.fileno())
                    if (info.st_uid, info.st_gid) != (temp_info.st_uid, temp_info.st_gid):
                        os.fchown(target.fileno(), info.st_uid, info.st_gid)
                os.fchmod(target.fileno(), current.mode)
                os.fsync(target.fileno())
            if self.read(destination).revision != expected:
                raise ConflictError("File changed during save; destination was not replaced")
            if expected == "missing":
                try:
                    os.link(temporary, destination)
                except FileExistsError as exc:
                    raise ConflictError("A file appeared during save; destination was not replaced") from exc
                os.unlink(temporary)
            else:
                os.replace(temporary, destination)
            directory_fd = os.open(str(Path(destination).parent), os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # Return the revision of what WE wrote, not a concurrent writer's contents.
            result = self.read(destination)
            if result.content != content:
                raise ConflictError("File changed immediately after save; editor contents preserved")
            return result
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


# Executed by the existing remote Bash/coreutils, never installed or persisted.
# Every parameter is passed via a quoted bash -c positional argument. Data is
# stdin, so shell metacharacters in filenames/content never become commands.
REMOTE_COMMON = r'''
set -e
export LC_ALL=C
p=$(readlink -m -- "$1" && printf .)
p=${p%.}
p=${p%$'\n'}
dir=${p%/*}
[ -n "$dir" ] || dir=/
revision() {
    if [ -e "$p" ]; then
        [ -f "$p" ] || { echo 'Only regular files can be edited' >&2; exit 1; }
        [ "$(stat -Lc %s -- "$p")" -le 8388608 ] || { echo 'File exceeds 8 MiB' >&2; exit 1; }
        printf '%s:%s' "$(stat -Lc '%d:%i:%s:%y:%z:%a:%u:%g' -- "$p")" "$(sha256sum < "$p" | cut -d ' ' -f 1)"
    else
        [ -d "$dir" ] || { echo 'Parent directory does not exist' >&2; exit 1; }
        printf missing
    fi
}
snapshot() {
    rev=$(revision)
    printf '%s\n' "$(printf %s "$p" | base64 -w0)" "$rev"
    if [ "$rev" = missing ]; then
        printf '600\n'
    else
        stat -Lc %a -- "$p"
        cat -- "$p"
    fi
    [ "$(revision)" = "$rev" ] || { echo 'File changed while being read' >&2; exit 73; }
}
'''
REMOTE_READ = REMOTE_COMMON + "\nsnapshot\n"
REMOTE_WRITE = REMOTE_COMMON + r'''
expected=$2
[ "$(revision)" = "$expected" ] || { echo 'File changed on server; editor contents preserved' >&2; exit 73; }
umask 077
tmp=$(mktemp -- "$dir/.naui-XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
cat > "$tmp"
[ "$(stat -c %s -- "$tmp")" -eq "$3" ] || { echo 'Incomplete transfer; destination unchanged' >&2; exit 1; }
if [ "$expected" != missing ]; then
    chown --reference="$p" -- "$tmp"
    chmod --reference="$p" -- "$tmp"
fi
[ "$(revision)" = "$expected" ] || { echo 'File changed during transfer; destination unchanged' >&2; exit 73; }
sync -f -- "$tmp"
if [ "$expected" = missing ]; then
    ln -- "$tmp" "$p" || { echo 'A file appeared during save; destination unchanged' >&2; exit 73; }
    rm -f -- "$tmp"
else
    mv -fT -- "$tmp" "$p"
fi
snapshot
'''


# Read-only media transport: the server runs only its existing Bash/coreutils.
# Limit stdout even if a concurrent writer grows the source during transfer.
REMOTE_PROBE = r'''
set -e
[ -f "$1" ] || { echo 'Only existing regular files can be previewed' >&2; exit 1; }
head -c "$2" -- "$1"
'''
REMOTE_PREVIEW = r'''
set -e
export LC_ALL=C
[ -f "$1" ] || { echo 'Only regular files can be previewed' >&2; exit 1; }
[ "$(stat -Lc %s -- "$1")" -le "$2" ] || { echo 'Remote preview exceeds its download limit' >&2; exit 1; }
before=$(stat -Lc '%d:%i:%s:%y:%z' -- "$1")
head -c "$(($2 + 1))" -- "$1"
[ "$(stat -Lc '%d:%i:%s:%y:%z' -- "$1")" = "$before" ] || { echo 'File changed during preview download' >&2; exit 73; }
'''


class RemoteFileBackend:
    def __init__(self, connection: Connection, authentication=None, device_id=None):
        self.connection = connection
        self.authentication, self.device_id = authentication, device_id
        self.connected = True
        self._lock = threading.Lock()
        self._closed = False
        self._processes = set()
        self._watch_hints = set()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.connected = False
            processes = list(self._processes)
            hints = list(self._watch_hints)
        # Do not leave OpenSSH running behind a cancelled Future. All new
        # process registration checks _closed under the same lock.
        for hint in hints:
            hint.close()
        for process in processes:
            stop_process(process)

    def run_command(self, argv, cwd, *, input_data=None):
        from .commands import REMOTE_COMMAND, validate_command, MAX_COMMAND_OUTPUT
        argv = validate_command(argv, cwd)
        if input_data is None:
            return self._run(REMOTE_COMMAND, [cwd, *argv], max_response=MAX_COMMAND_OUTPUT, stream=True, timeout=20)
        if not isinstance(input_data, bytes) or len(input_data) > MAX_COMMAND_OUTPUT:
            raise ValueError('Command input exceeds 8 MiB')
        with tempfile.TemporaryFile() as source:
            source.write(input_data); source.seek(0)
            return self._run(REMOTE_COMMAND, [cwd, *argv], max_response=MAX_COMMAND_OUTPUT, stream=True, timeout=20, input_source=source)

    def watch_hint(self, path):
        from .remote_watch import RemoteWatchHint
        return RemoteWatchHint(self, path)

    def watch_snapshot(self, path):
        from .watch_snapshot import REMOTE_WATCH_SNAPSHOT, parse_watch_snapshot
        return parse_watch_snapshot(self._run(REMOTE_WATCH_SNAPSHOT, [path], max_response=4*1024*1024, stream=True), path)

    def transfer_properties(self, path):
        data = self._run(REMOTE_PROPERTIES, [path])
        if not data:
            return None
        try:
            entries = parse_entries(data, os.path.dirname(path.rstrip('/')) or '/', root=path == '/')
            if len(entries) != 1:
                raise ValueError('Invalid transfer properties')
            return entries[0]
        except (ValueError, UnicodeError) as error:
            raise FileError('Invalid transfer properties response') from error

    def available_destination(self, path):
        from .transfer_io import available_destination
        return available_destination(self, path)

    def iter_directory(self, path, control, expected=None):
        from .transfer_io import remote_entries
        return remote_entries(self, path, control, expected)

    def transfer_reader(self, path, info, control):
        from .transfer_io import remote_reader
        return remote_reader(self, path, info, control)

    def transfer_writer(self, path, info, expected, control):
        from .transfer_io import remote_writer
        return remote_writer(self, path, info, expected, control)

    def probe(self, path):
        data = self._run(REMOTE_PROBE, [path, str(PROBE_BYTES)], max_response=PROBE_BYTES, stream=True)
        return FileProbe(path, data)

    def copy_to(self, path, destination, limit):
        self._run(REMOTE_PREVIEW, [path, str(limit)], output_path=destination,
                  max_response=limit, timeout=120)

    def list_directory(self, path):
        try:
            return parse_entries(self._run(REMOTE_LIST, [path]), path)
        except (ValueError, UnicodeError) as exc:
            raise FileError("Invalid remote directory response: " + str(exc)) from exc

    def properties(self, path):
        try:
            entries = parse_entries(self._run(REMOTE_PROPERTIES, [path]),
                                    os.path.dirname(path.rstrip("/")) or "/", root=path == "/")
            if len(entries) != 1:
                raise ValueError("Selection no longer exists")
            return entries[0]
        except (ValueError, UnicodeError) as exc:
            raise FileError("Invalid remote properties response: " + str(exc)) from exc

    def create(self, path, directory=False):
        self._run(REMOTE_CREATE, [path, "directory" if directory else "file"])

    def rename(self, path, destination, expected):
        self._run(REMOTE_MUTATE, [path, expected, "rename", destination])

    def delete(self, path, expected):
        self._run(REMOTE_MUTATE, [path, expected, "delete"])

    def _run(self, code, args, data=None, *, output_path=None,
             max_response=MAX_FILE_SIZE + 16384, timeout=30, stream=False, input_source=None):
        if not self.connected:
            raise DisconnectedError("SSH session is disconnected. Copy your edits before closing this tab.")
        control = CURRENT_TRANSFER.get()
        if control:
            control.check()
        if self.authentication:
            timeout = max(timeout, 180)  # Allow a human to answer an askpass prompt.
        script = "bash -c " + shlex.quote(code) + " naui " + " ".join(shlex.quote(x) for x in args)
        # Passwords/host-key prompts belong to the interactive ssh PTY. Reuse a
        # temporary local ControlMaster from that session; otherwise use keys/agent.
        try:
            with (self.authentication.operation(self.device_id, control) if self.authentication else nullcontext(ssh_environment())) as environment, (open(output_path, "wb") if output_path else tempfile.TemporaryFile()) as output, tempfile.TemporaryFile() as errors:
                with self._lock:
                    if self._closed or not self.connected:
                        raise DisconnectedError("SSH operation cancelled; editor contents preserved")
                    process = subprocess.Popen(self.connection.command(script, batch=self.authentication is None),
                                               stdin=input_source if input_source is not None else subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                               stdout=subprocess.PIPE if output_path or stream else output,
                                               stderr=errors, env=environment)
                    self._processes.add(process)
                try:
                    try:
                        if output_path or stream:
                            self._stream_preview(process, output, max_response, timeout)
                        elif control and data is None:
                            deadline = time.monotonic() + timeout
                            while process.poll() is None:
                                deadline += control.check() or 0
                                if time.monotonic() > deadline:
                                    raise subprocess.TimeoutExpired(process.args, timeout)
                                time.sleep(0.02)
                        else:
                            process.communicate(input=data, timeout=timeout)
                    finally:
                        stop_process(process)
                        if (output_path or stream) and process.stdout:
                            process.stdout.close()
                finally:
                    with self._lock:
                        self._processes.discard(process)
                if self._closed:
                    raise DisconnectedError("SSH operation cancelled; editor contents preserved")
                errors.seek(0)
                error = errors.read(8192).decode("utf-8", "replace").strip()
                if process.returncode == 255:
                    self.connected = False
                    raise DisconnectedError(error or "OpenSSH connection failed; editor contents preserved")
                if process.returncode == 73:
                    raise ConflictError(error)
                if process.returncode:
                    raise FileError(error or f"Remote file operation failed ({process.returncode})")
                if output.tell() > max_response:
                    raise FileError("Remote response exceeds the transfer limit")
                if output_path:
                    return None
                output.seek(0)
                result = output.read(max_response + 1)
                if len(result) > max_response:
                    raise FileError("Remote response exceeds the transfer limit")
                return result
        except subprocess.TimeoutExpired as exc:
            self.connected = False
            raise DisconnectedError("OpenSSH operation timed out; editor contents preserved") from exc

    def _stream_preview(self, process, target, limit, timeout):
        # Enforce preview/probe caps LOCALLY, even if the remote endpoint
        # ignores our head/stat command. Do not allocate the full media in RAM.
        deadline, received = time.monotonic() + timeout, 0
        while True:
            if self._closed:
                raise DisconnectedError("Preview transfer cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            ready, _, _ = select.select([process.stdout], [], [], min(0.1, remaining))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            received += len(chunk)
            if received > limit:
                raise FileError("Remote preview exceeds the download limit")
            target.write(chunk)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))

    @staticmethod
    def _snapshot(data):
        import base64
        try:
            encoded, revision, mode, content = data.split(b"\n", 3)
            path = base64.b64decode(encoded, validate=True).decode("utf-8")
            if not path.startswith("/") or "\0" in path:
                raise ValueError("Invalid path")
            return FileSnapshot(path, _decode(content), revision.decode("ascii"), int(mode, 8))
        except (ValueError, UnicodeError) as exc:
            raise FileError("Invalid remote file response") from exc

    def read(self, path):
        return self._snapshot(self._run(REMOTE_READ, [path]))

    def stat(self, path):
        return self.read(path)

    def exists(self, path):
        return self.read(path).revision != "missing"

    def write(self, path, content, expected):
        data = _encode(content)
        result = self._snapshot(self._run(REMOTE_WRITE, [path, expected, str(len(data))], data))
        if result.content != content:
            raise ConflictError("File changed immediately after save; editor contents preserved")
        return result

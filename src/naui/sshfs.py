"""Optional, device-owned, read-only SSHFS roots. No synchronization or GTK.

OpenSSH still owns authentication. Mutations and revision-sensitive operations
use the compatibility transport: FUSE inode IDs are not remote inode IDs, and
not every SFTP server offers atomic replacement. Mounts are never tab-owned.
"""
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .backends import LocalFileBackend, FileError
from .preview import FileProbe
from .processes import stop_process
from .ssh import Connection, ssh_environment, validate_transport_options

MODES = ('automatic', 'sshfs', 'compatibility')


@dataclass(frozen=True)
class SSHFSSupport:
    executable: str | None
    unmount_tool: str | None
    usable: bool
    reason: str
    version: str = ''


def detect_support():
    executable = shutil.which('sshfs')
    unmount = shutil.which('fusermount3') or shutil.which('fusermount')
    if sys.platform != 'linux':
        return SSHFSSupport(executable, unmount, False, 'SSHFS integration is currently Linux-only')
    if not executable or not unmount:
        return SSHFSSupport(executable, unmount, False, 'SSHFS or the FUSE unmount tool is unavailable')
    if not os.access('/dev/fuse', os.R_OK | os.W_OK):
        return SSHFSSupport(executable, unmount, False, 'FUSE device is unavailable or inaccessible')
    try:
        result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=3)
        version = next((line for line in result.stdout.splitlines() if line.startswith('SSHFS version')), '')[:80]
    except (OSError, subprocess.SubprocessError):
        version = ''
    return SSHFSSupport(executable, unmount, True, '', version)


def logical_path(path):
    if not isinstance(path, str) or not path.startswith('/') or '\0' in path or len(path.encode()) > 4096:
        raise ValueError('Expected an absolute device path')
    # Reject traversal rather than allowing normpath to conceal it.
    if '..' in path.split('/'):
        raise ValueError('Device path contains parent traversal')
    return posixpath.normpath(path)


def within(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def device_connection(connection):
    """Keep normal transport settings, drop terminal-owned multiplex sockets."""
    validate_transport_options(connection.options)
    if any(not isinstance(value, str) or '\0' in value for value in connection.options):
        raise ValueError('Invalid SSH transport argument')
    options, index = [], 0
    while index < len(connection.options):
        flag = connection.options[index]
        pair = connection.options[index:index+2]
        if flag == '-S':
            index += 2
            continue
        if flag == '-o' and re.split(r'[=\s]', pair[1], maxsplit=1)[0].lower() in {
                'controlpath', 'controlmaster', 'controlpersist'}:
            index += 2
            continue
        count = 1 if flag in {'-4', '-6', '-C', '-v', '-vv', '-vvv', '-q', '-A', '-a'} else 2
        options.extend(connection.options[index:index+count])
        index += count
    return Connection(connection.host, tuple(options))


def mount_command(support, connection, root, mountpoint):
    connection = device_connection(connection)
    # sshfs parses ssh_command using its own quoted-argument parser, not a
    # shell. Commas belong to FUSE's option grammar; don't pass ambiguous ones.
    ssh = shlex.join(['ssh', '-T', '-o', 'RequestTTY=no', '-o', 'RemoteCommand=none',
                      '-o', 'SessionType=default', '-o', 'ClearAllForwardings=yes',
                      '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                      '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=5',
                      '-o', 'ServerAliveCountMax=2', *connection.options])
    if ',' in ssh or any(c in connection.host for c in '\0\n\r') or connection.host.startswith('-'):
        raise ValueError('SSH transport cannot be represented safely for SSHFS')
    # The directory is SFTP protocol data, not a remote shell command.
    destination = connection.host
    prefix, separator, hostname = destination.rpartition('@')
    hostname = hostname if separator else destination
    if ':' in hostname and not hostname.startswith('['):
        destination = (prefix + '@' if separator else '') + '[' + hostname + ']'
    source = destination + ':' + logical_path(root)
    return [support.executable, '-f', source, str(mountpoint), '-o', 'ro', '-o', 'nosuid,nodev,noexec',
            '-o', 'follow_symlinks', '-o', 'dir_cache=no', '-o', 'attr_timeout=0',
            '-o', 'entry_timeout=0', '-o', 'negative_timeout=0', '-o', 'no_readahead',
            '-o', 'ssh_command=' + ssh]


@dataclass
class Mount:
    root: str
    path: Path
    process: object


class SSHFSFileBackend:
    """Lazy selected roots and explicit logical/physical translation.

    Symlinks are resolved on the server by SSHFS, never as local absolute links.
    Each directory expansion still has a strict 10,000-entry limit. Metadata
    revisions come from SSH so rename/delete retain their original safeguards.
    """
    def __init__(self, device_id, connection, authentication=None, *, support=None, base=None):
        if not isinstance(device_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', device_id):
            raise ValueError('Invalid mount device identity')
        self.device_id, self.connection, self.authentication = device_id, device_connection(connection), authentication
        self.support = support or detect_support()
        self._base = Path(base) if base else None
        self._directory = None
        self._mounts, self._failed, self._pending = {}, set(), set()
        self._roots = ()
        self._lock = threading.RLock()
        self._config_lock = threading.Lock()
        self._closed = threading.Event()
        self.warning = self.support.reason
        self.notice = ''
        self.local = LocalFileBackend()

    @property
    def mounted(self):
        # A UI status/lifecycle query must not wait for mount authentication.
        return bool(self._mounts)

    @property
    def in_use(self):
        return bool(self._mounts or self._pending)

    def set_roots(self, roots):
        roots = tuple(dict.fromkeys(logical_path(path) for path in roots))
        if len(roots) > 32:
            raise ValueError('Too many SSHFS roots')
        with self._config_lock:
            self._roots = roots

    def cleanup_removed(self):
        # Metadata changes are immediate on the UI thread. FUSE I/O is deferred
        # to a file worker, so an authentication wait cannot freeze root controls.
        with self._lock:
            for root in tuple(self._mounts):
                if root not in self._roots:
                    self.unmount(root)

    def _runtime(self):
        if self._directory is not None:
            return self._directory
        configured = os.environ.get('XDG_RUNTIME_DIR')
        base = self._base or (Path(configured) / 'naui/mounts' if configured and Path(configured).is_absolute()
                              else Path(tempfile.gettempdir()) / ('naui-' + str(os.getuid())) / 'mounts')
        # Avoid shared/symlinked runtime parents. Never recursively delete these.
        for parent in (base.parent, base):
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = parent.lstat()
            if parent.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FileError('SSHFS needs a private user-owned runtime directory')
        # Detect, but never unmount/delete, directories from other sessions.
        # A dead PID is not proof that its mount has no other users.
        for old in base.iterdir():
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}-[a-zA-Z0-9_]{8}', old.name):
                continue
            try:
                marker = old/'owner.json'
                if old.is_symlink() or marker.is_symlink() or marker.stat().st_size > 1024:
                    continue
                record = json.loads(marker.read_text())
                if record.get('format') != 1 or record.get('uid') != os.getuid():
                    continue
                os.kill(record['pid'], 0)
            except ProcessLookupError:
                self.notice = 'An older Naui mount runtime remains; owner inspection is required'
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self._directory = Path(tempfile.mkdtemp(prefix=self.device_id + '-', dir=base))
        marker = self._directory/'owner.json'
        with marker.open('x') as output:
            json.dump({'format':1,'uid':os.getuid(),'pid':os.getpid()}, output)
        marker.chmod(0o600)
        return self._directory

    def local_path(self, path):
        path = logical_path(path)
        with self._lock:
            if self._closed.is_set() or not self.support.usable:
                return None
            self.cleanup_removed()
            roots = [root for root in self._roots if within(path, root)]
            if not roots:
                return None  # Unpinned locations use SSH, never an implicit / mount.
            root = max(roots, key=len)
            mount = self._mounts.get(root)
            if mount and mount.process.poll() is not None:
                self.warning = 'SSHFS connection ended; using SSH compatibility access'
                return None
            if mount is None:
                if root in self._failed:
                    return None
                mount = self._mount(root)
                if mount is None:
                    return None
            if root not in self._roots:
                self.cleanup_removed()
                return None
            candidate = os.path.join(str(mount.path), posixpath.relpath(path, root))
            resolved = os.path.realpath(candidate)
            if not within(resolved, str(mount.path)):
                raise FileError('Remote symlink escaped its mounted root')
            return resolved

    def device_path(self, path):
        physical = os.path.realpath(path)
        with self._lock:
            for mount in self._mounts.values():
                if within(physical, str(mount.path)):
                    return logical_path(posixpath.join(mount.root, os.path.relpath(physical, mount.path)))
        raise ValueError('Path does not belong to this device mount')

    def _mount(self, root):
        process = None
        try:
            mountpoint = self._runtime() / hashlib.sha256(root.encode()).hexdigest()[:24]
            mountpoint.mkdir(mode=0o700)
            environment = self.authentication.operation(self.device_id) if self.authentication else nullcontext(ssh_environment())
            with environment as env:
                process = subprocess.Popen(mount_command(self.support, self.connection, root, mountpoint),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, env=env, start_new_session=True)
                self._pending.add(process)
                deadline = time.monotonic() + 30
                while process.poll() is None and not self._closed.is_set():
                    if os.path.ismount(mountpoint):
                        mount = Mount(root, mountpoint, process)
                        self._mounts[root] = mount
                        self.warning = ''
                        return mount
                    if time.monotonic() > deadline:
                        break
                    self._closed.wait(.05)
            self.warning = 'SSHFS mount failed or authentication was cancelled; using SSH compatibility access'
        except (OSError, ValueError, RuntimeError):
            self.warning = 'SSHFS mount unavailable; using SSH compatibility access'
        finally:
            if process:
                self._pending.discard(process)
                if root not in self._mounts:
                    stop_process(process)
        self._failed.add(root)  # Retry only after explicit reconnect/mode reset.
        return None

    def probe(self, path):
        physical = self.local_path(path)
        if physical is None:
            return None
        try:
            result = self.local.probe(physical)
            return FileProbe(logical_path(path), result.sample, result.exists)
        except OSError:
            raise FileError('Cannot preview remote file: ' + logical_path(path)) from None

    def preview_path(self, path):
        physical = self.local_path(path)
        if physical is not None:
            self.probe(path)  # Reject FIFO/device/directory before codecs see it.
        return physical

    def list_directory(self, path, compatibility):
        physical = self.local_path(path)
        if physical is None:
            return compatibility.list_directory(path)
        try:
            names = {entry.name for entry in self.local.list_directory(physical)}
        except OSError:
            raise FileError('Cannot list remote directory: ' + logical_path(path)) from None
        # One bounded metadata query, not one SSH command per file. Native
        # FUSE revisions cannot safely be used with server-side destructive ops.
        return [entry for entry in compatibility.list_directory(path) if entry.name in names]

    def cancel(self):
        self._closed.set()
        # Killing our read-only FUSE process releases blocked native readers.
        # Leave mount directories in place until normal unmount succeeds.
        for process in tuple(self._pending):
            stop_process(process)
        for mount in tuple(self._mounts.values()):
            stop_process(mount.process)

    def unmount(self, root):
        with self._lock:
            mount = self._mounts.get(root)
            if mount is None:
                return True
            try:
                if os.path.ismount(mount.path):
                    result = subprocess.run([self.support.unmount_tool, '-u', str(mount.path)],
                                            capture_output=True, timeout=5)
                    if result.returncode or os.path.ismount(mount.path):
                        self.warning = 'SSHFS root is busy or stale; its runtime directory was retained'
                        return False
                stop_process(mount.process)
                mount.path.rmdir()  # Empty, unmounted directory only.
                self._mounts.pop(root)
                return True
            except (OSError, subprocess.SubprocessError):
                self.warning = 'SSHFS cleanup incomplete; its runtime directory was retained'
                return False

    def close(self):
        self.cancel()
        with self._lock:
            for root in tuple(self._mounts):
                self.unmount(root)
            if self._directory and not self._mounts:
                try:
                    # Failed mount attempts leave only empty hash directories.
                    for path in self._directory.iterdir():
                        if re.fullmatch(r'[a-f0-9]{24}', path.name) and not path.is_symlink() and not os.path.ismount(path):
                            path.rmdir()
                    (self._directory/'owner.json').unlink(missing_ok=True)
                    self._directory.rmdir()
                    self._directory = None
                except OSError:
                    self.warning = 'SSHFS runtime directory retained for inspection'

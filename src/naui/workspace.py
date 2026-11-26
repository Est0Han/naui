"""Frontend-independent devices, terminal state and safe workspace persistence.

Only connection names/aliases and root references are persisted. Live OpenSSH
options (including ControlMaster sockets), authentication, jobs and GUI objects
never enter the config file. A remote device has a stable backend identity;
reconnect replaces its transport, not a document's device or save destination.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import socket
import tempfile
import threading
import uuid

from .backends import DisconnectedError, LocalFileBackend, RemoteFileBackend
from .navigation import NavigationHistory
from .ssh import Connection

MAX_DEVICES, MAX_ROOTS, MAX_CONFIG = 32, 32, 1024 * 1024


def ssh_host(value):
    """A single OpenSSH destination, never options or a remote command."""
    if (not isinstance(value, str) or not value or len(value.encode()) > 255
            or value.startswith("-") or not re.fullmatch(r"[\w.@:\[\]+-]+", value, re.ASCII)):
        raise ValueError("Enter an SSH alias, hostname or user@host (without options/spaces).")
    return value


def root_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or "\0" in value
            or len(value.encode("utf-8")) > 4096):
        raise ValueError("Workspace roots must be absolute paths without NUL bytes.")
    return posixpath.normpath(value)


def display_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value.encode()) > 128
            or any(ord(character) < 32 for character in value)):
        raise ValueError("Choose a nonempty device name of at most 128 bytes without controls.")
    return value.strip()


def connection_metadata(connection):
    """Validate just the safe destination fields before registering a device."""
    ssh_host(connection.host)
    username = connection.username
    if username is not None and (not isinstance(username, str)
            or not re.fullmatch(r"[\w.+-]{1,128}", username, re.ASCII)):
        raise ValueError("Invalid SSH username.")
    options = connection.options
    port = options[options.index("-p") + 1] if "-p" in options else None
    if port is not None and (not isinstance(port, str) or not port.isascii()
            or not port.isdecimal() or not 1 <= int(port) <= 65535):
        raise ValueError("Invalid SSH port.")
    return username, port


class RemoteDeviceBackend:
    """Stable device identity with a replaceable, cancellable OpenSSH transport.

    Each operation takes one delegate. An old in-flight operation can fail on
    disconnect, but cannot migrate halfway to another connection. Text writes
    still require the existing backend's optimistic revision check.
    """
    def __init__(self, host):
        self.connection = Connection(ssh_host(host))
        self._delegate = None
        self._device_delegate = None
        self._sessions = {}
        self._lock = threading.RLock()
        self.generation = 0
        self._closed = True
        self._identity = None
        self.authentication, self.device_id = None, None
        self.filesystem_mode = 'automatic'
        self._roots = ()
        self._sshfs = None
        self._retired_mounts = []

    def set_roots(self, roots):
        with self._lock:
            self._roots = tuple(roots)
            mounts = self._sshfs
        if mounts:
            mounts.set_roots(roots)

    def cleanup_removed_mounts(self):
        with self._lock:
            mounts = self._sshfs
        if mounts:
            mounts.cleanup_removed()

    def set_filesystem_mode(self, mode):
        from .sshfs import MODES
        if mode not in MODES:
            raise ValueError('Unknown remote filesystem mode')
        with self._lock:
            old, self._sshfs = self._sshfs, None
            self.filesystem_mode = mode
            if old:
                old.cancel()
                self._retired_mounts.append(old)
            self.generation += 1
        self.cleanup_mounts()

    @property
    def filesystem_label(self):
        """Cheap UI metadata; never probe tools or mount while rendering a row."""
        with self._lock:
            return 'SSHFS' if self._sshfs and self._sshfs.mounted and not self._sshfs.warning else 'Compatibility'

    @property
    def filesystem_status(self):
        from .sshfs import detect_support
        with self._lock:
            mounts = self._sshfs
            if mounts:
                status = ('SSHFS (read access; protected writes via SSH)' if mounts.mounted and not mounts.warning
                          else 'SSH compatibility fallback: ' + (mounts.warning or 'SSHFS roots mount on demand'))
                return status + ('\n' + mounts.notice if mounts.notice else '')
            if self._retired_mounts and any(mount.mounted for mount in self._retired_mounts):
                return 'SSHFS cleanup incomplete: busy/stale mount retained for owner inspection'
        if self.filesystem_mode == 'compatibility':
            return 'SSH compatibility (selected)'
        support = detect_support()
        return 'SSHFS roots mount on demand' if support.usable else 'SSH compatibility fallback: ' + support.reason

    def _mounted_backend(self):
        from .sshfs import SSHFSFileBackend
        with self._lock:
            if self._closed or self.filesystem_mode == 'compatibility' or not self.device_id:
                return None
            if self._sshfs is None:
                self._sshfs = SSHFSFileBackend(self.device_id, self.connection, self.authentication)
                self._sshfs.set_roots(self._roots)
            return self._sshfs

    def local_path(self, path):
        mounts = self._mounted_backend()
        return mounts.local_path(path) if mounts else None

    def preview_path(self, path):
        mounts = self._mounted_backend()
        try:
            return mounts.preview_path(path) if mounts else None
        except OSError:
            if mounts:
                mounts.warning = 'SSHFS preview unavailable; using bounded download fallback'
            return None

    @property
    def needs_mount_cleanup(self):
        return any(mount.mounted or mount._directory is not None for mount in self._retired_mounts)

    def cleanup_mounts(self):
        # Called after file/media/LSP workers release their paths on app exit.
        with self._lock:
            mounts, self._retired_mounts = self._retired_mounts, []
        retained = []
        for mount in mounts:
            mount.close()
            if mount.mounted:
                retained.append(mount)
        with self._lock:
            self._retired_mounts.extend(retained)

    @property
    def connected(self):
        with self._lock:
            return self._delegate is not None and self._delegate.connected

    def connect(self, connection, session=None):
        # Called only after a locally signed session-start from this device's
        # OpenSSH helper. A separate device is used for a different destination.
        with self._lock:
            username, port = connection_metadata(connection)
            identity = (connection.host, username, port)
            if connection.host != self.connection.host or (self._identity is not None and identity != self._identity):
                raise ValueError("Cannot retarget an existing device backend to a different SSH identity.")
            # Legacy reconnect replaces the transport. Interactive terminals
            # instead register individually owned OpenSSH control sockets.
            if session is None:
                self.close()
            elif session in self._sessions:
                raise ValueError('SSH session already registered')
            delegate = RemoteFileBackend(connection, self.authentication, self.device_id)
            self._sessions[session] = delegate
            # Registration precedes interactive authentication. Keep using an
            # existing master while the new terminal is still connecting.
            if self._delegate is None or not self._delegate.connected:
                self.connection, self._delegate = connection, delegate
                self.generation += 1
            self._identity = identity
            self._closed = False

    def has_session(self, session):
        with self._lock:
            return session in self._sessions and self._sessions[session].connected

    def select(self, session):
        with self._lock:
            delegate = self._sessions.get(session)
            if delegate is None or not delegate.connected:
                return False
            if delegate is not self._delegate:
                self._delegate, self.connection = delegate, delegate.connection
                self.generation += 1
            return True

    def release(self, session):
        """Release one terminal's transport; retain unrelated live transports."""
        with self._lock:
            delegate = self._sessions.pop(session, None)
            if delegate is None:
                return self.connected
            if delegate is self._delegate:
                self._delegate = next((value for value in reversed(list(self._sessions.values())) if value.connected), None)
                if self._delegate:
                    self.connection = self._delegate.connection
                elif self._sshfs and self._sshfs.in_use:
                    # A device-owned filesystem outlives its final terminal.
                    # Use a separate SSH transport without that tab's socket.
                    from .sshfs import device_connection
                    self.connection = device_connection(self.connection)
                    if self._device_delegate is None or not self._device_delegate.connected:
                        if self._device_delegate:
                            self._device_delegate.close()
                        self._device_delegate = RemoteFileBackend(self.connection, self.authentication, self.device_id)
                    self._delegate = self._device_delegate
                self.generation += 1
            delegate.close()
            self._closed = self._delegate is None
            return self.connected

    def close(self):
        with self._lock:
            if self._closed and self._sshfs is None:
                return
            if self._delegate:
                for delegate in self._sessions.values():
                    delegate.close()
                self._delegate.close()
                if self._device_delegate:
                    self._device_delegate.close()
                    self._device_delegate = None
                self.generation += 1
            self._sessions.clear()
            self._closed = True
            if self._sshfs:
                self._sshfs.cancel()
                self._retired_mounts.append(self._sshfs)
                self._sshfs = None

    def _operation(self, name, *args, **kwargs):
        with self._lock:
            delegate = self._delegate
        if delegate is None or not delegate.connected:
            raise DisconnectedError("Device disconnected. Reconnect it; document contents are preserved.")
        return getattr(delegate, name)(*args, **kwargs)

    def transfer_properties(self, path): return self._operation("transfer_properties", path)
    def available_destination(self, path): return self._operation("available_destination", path)
    def iter_directory(self, path, control, expected=None): return self._operation("iter_directory", path, control, expected)
    def transfer_reader(self, path, info, control): return self._operation("transfer_reader", path, info, control)
    def transfer_writer(self, path, info, expected, control): return self._operation("transfer_writer", path, info, expected, control)

    def read(self, path): return self._operation("read", path)
    def write(self, path, content, expected): return self._operation("write", path, content, expected)
    def exists(self, path): return self._operation("exists", path)
    def stat(self, path): return self._operation("stat", path)
    def probe(self, path):
        mounts = self._mounted_backend()
        try:
            probe = mounts.probe(path) if mounts else None
        except OSError:
            probe = None
        return probe if probe is not None else self._operation('probe', path)
    def copy_to(self, path, destination, limit): return self._operation("copy_to", path, destination, limit)
    def list_directory(self, path):
        with self._lock:
            delegate = self._delegate
        if delegate is None or not delegate.connected:
            raise DisconnectedError('Device disconnected; reconnect to browse files')
        mounts = self._mounted_backend()
        try:
            return mounts.list_directory(path, delegate) if mounts else delegate.list_directory(path)
        except OSError:
            if mounts:
                mounts.warning = 'SSHFS read failed; using SSH compatibility access'
            return delegate.list_directory(path)
    def properties(self, path): return self._operation("properties", path)
    def run_command(self, argv, cwd, **options): return self._operation("run_command", argv, cwd, **options)
    def watch_hint(self, path): return self._operation("watch_hint", path)
    def watch_snapshot(self, path): return self._operation("watch_snapshot", path)
    def create(self, path, directory=False): return self._operation("create", path, directory=directory)
    def rename(self, path, destination, expected): return self._operation("rename", path, destination, expected)
    def delete(self, path, expected): return self._operation("delete", path, expected)


@dataclass
class NavigationState:
    current_path: str | None = None
    home: str | None = None
    history: NavigationHistory = field(default_factory=NavigationHistory)
    expanded: set = field(default_factory=set)


@dataclass
class WorkspaceRoot:
    path: str
    navigation: NavigationState = field(default_factory=NavigationState)


@dataclass
class Device:
    id: str
    type: str
    display_name: str
    hostname: str
    username: str | None
    filesystem_backend: object
    roots: list = field(default_factory=list)
    navigation_state: NavigationState = field(default_factory=NavigationState)
    terminal_sessions: list = field(default_factory=list)
    active_terminal_id: str | None = None
    state: str = "disconnected"
    session_id: str | None = None
    # Safe reconnect arguments only; never persist live/authentication options.
    port: str | None = None

    @property
    def connected(self):
        return self.type == "local" or (self.state == "connected" and self.filesystem_backend.connected)

    def add_root(self, path):
        path = root_path(path)
        if any(root.path == path for root in self.roots):
            return
        if len(self.roots) >= MAX_ROOTS:
            raise ValueError("A device can have at most 32 pinned roots.")
        root = WorkspaceRoot(path)
        root.navigation.current_path = path
        root.navigation.history.observe(path)
        self.roots.append(root)
        if self.type == 'ssh':
            self.filesystem_backend.set_roots([root.path for root in self.roots])

    def remove_root(self, path):
        self.roots[:] = [root for root in self.roots if root.path != path]
        if self.type == 'ssh':
            self.filesystem_backend.set_roots([root.path for root in self.roots])

    def observe(self, path, home=None):
        state = self.navigation_state
        path = root_path(path)
        state.current_path = path
        state.home = home or state.home
        # UI activation/page changes may show an unchanged cwd while a Back
        # request is awaiting its PTY acknowledgment. Duplicate observations
        # must not consume NavigationHistory.pending_index.
        if state.history.current != path:
            state.history.observe(path)
        for root in self.roots:
            if path == root.path or path.startswith(root.path.rstrip("/") + "/"):
                root.navigation.current_path = path
                root.navigation.home = state.home
                if root.navigation.history.current != path:
                    root.navigation.history.observe(path)

    def reconnect_args(self):
        args = []
        if self.port:
            args += ["-p", self.port]
        if self.username and self.username != Connection(self.hostname).username:
            args += ["-l", self.username]
        return [*args, self.hostname]

    def history_for_path(self, path):
        candidates = [root for root in self.roots if path and
                      (path == root.path or path.startswith(root.path.rstrip("/") + "/"))]
        return max(candidates, key=lambda root: len(root.path)).navigation.history if candidates else self.navigation_state.history


@dataclass
class TerminalState:
    """No native objects: more than one terminal can reference the same device."""
    id: str
    base_device_id: str
    device_id: str
    contexts: dict
    active_session: str | None = None
    at_prompt: bool = False
    navigation_pending: dict | None = None
    navigation_timeout: int | None = None
    closed: bool = False
    closing: bool = False
    resume_path: str | None = None


class Workspace:
    def __init__(self, cwd, records=()):
        self.devices = {}
        local = Device("local", "local", socket.gethostname(), socket.gethostname(), None,
                       LocalFileBackend(), state="connected")
        local.observe(cwd, os.path.expanduser("~"))
        self.devices[local.id] = local
        for record in records:
            if record["type"] == "local":
                device = local
                device.display_name = record["display_name"]
            else:
                host = record["hostname"]
                device = Device(record["id"], "ssh", record["display_name"], host,
                                record.get("username"), RemoteDeviceBackend(host), port=record.get("port"))
                device.filesystem_backend.filesystem_mode = record.get('filesystem_mode', 'automatic')
                self.devices[device.id] = device
            for path in record["roots"]:
                device.add_root(path)
            if device.type == "ssh" and device.roots:
                device.observe(device.roots[0].path)
        if not records:
            local.add_root(cwd)
        self.active_device_id = local.id

    def add_ssh(self, host, name=None):
        if len(self.devices) >= MAX_DEVICES:
            raise ValueError("A workspace can contain at most 32 devices.")
        host = ssh_host(host)
        connection = Connection(host)
        username, _ = connection_metadata(connection)
        device = Device("ssh-" + uuid.uuid4().hex, "ssh", display_name(name or host), host,
                        username, RemoteDeviceBackend(host))
        self.devices[device.id] = device
        return device

    def device_for_backend(self, backend):
        return next((device for device in self.devices.values() if device.filesystem_backend is backend), None)

    def records(self):
        return [{"id": device.id, "type": device.type, "display_name": device.display_name,
                 "hostname": device.hostname if device.type == "ssh" else "",
                 "username": device.username if device.type == "ssh" else None,
                 "port": device.port if device.type == "ssh" else None,
                 "filesystem_mode": device.filesystem_backend.filesystem_mode if device.type == 'ssh' else 'automatic',
                 "roots": [root.path for root in device.roots]} for device in self.devices.values()]


def config_path():
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not directory.is_absolute():
        directory = Path.home() / ".config"
    return directory / "naui/workspace.json"


class WorkspaceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else config_path()

    def load(self):
        try:
            if self.path.stat().st_size > MAX_CONFIG:
                raise ValueError("Workspace config exceeds 1 MiB.")
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        return self.validate(data)

    @staticmethod
    def validate(data):
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("devices"), list) or len(data["devices"]) > MAX_DEVICES):
            raise ValueError("Invalid workspace configuration.")
        result, ids = [], set()
        for record in data["devices"]:
            if not isinstance(record, dict):
                raise ValueError("Invalid device record.")
            identity, kind = record.get("id"), record.get("type")
            if (kind not in {"local", "ssh"} or not isinstance(identity, str)
                    or not re.fullmatch(r"local|ssh-[a-f0-9]{32}", identity)
                    or (identity == "local") != (kind == "local") or identity in ids):
                raise ValueError("Invalid or duplicate device identity.")
            ids.add(identity)
            roots = record.get("roots")
            if not isinstance(roots, list) or len(roots) > MAX_ROOTS:
                raise ValueError("Invalid workspace roots.")
            host = ssh_host(record.get("hostname")) if kind == "ssh" else ""
            username = record.get("username")
            port = record.get("port")
            mode = record.get('filesystem_mode', 'automatic')
            from .sshfs import MODES
            if mode not in MODES:
                raise ValueError('Invalid remote filesystem mode')
            metadata = (() if username is None else ("-l", username)) + (() if port is None else ("-p", port))
            connection_metadata(Connection(host or "localhost", metadata))
            result.append({"id": identity, "type": kind, "display_name": display_name(record.get("display_name")),
                           "hostname": host, "username": username, "port": port,
                           "filesystem_mode": mode,
                           "roots": [root_path(path) for path in roots]})
        return result

    def save(self, workspace):
        record = {"version": 1, "devices": workspace.records()}
        self.validate(record)
        data = json.dumps(record, ensure_ascii=False, indent=2).encode() + b"\n"
        if len(data) > MAX_CONFIG:
            raise ValueError("Workspace config exceeds 1 MiB.")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".workspace-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def ssh_config_hosts(path=None):
    """Best-effort alias suggestions; never read IdentityFile/key contents.

    Includes/wildcards/negation remain OpenSSH's job, not a second config parser.
    """
    path = Path(path) if path else Path.home() / ".ssh/config"
    try:
        if path.stat().st_size > MAX_CONFIG:
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    aliases = []
    for line in lines:
        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            continue
        if fields and fields[0].lower() == "host":
            for alias in fields[1:]:
                try:
                    alias = ssh_host(alias)
                except ValueError:
                    continue
                if alias not in aliases:
                    aliases.append(alias)
    return aliases[:MAX_DEVICES * 4]

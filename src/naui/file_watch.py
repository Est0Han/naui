"""Shared nonrecursive native/local and adaptive SSH directory watching.

Subscriptions and callbacks live on the main thread. Workers own only backend
objects/plain data. Remote snapshots inspect one relevant directory; optional
transient inotify hints wake polling without parsing filenames from stdout.
"""
from dataclasses import dataclass, field
import posixpath
import time
from .watch_snapshot import REMOTE_WATCH_SNAPSHOT, parse_watch_snapshot

from gi.repository import Gio, GLib

from .lifecycle import FileWork
from .transfers import FileLocation


@dataclass(frozen=True)
class FileChangeEvent:
    device_id: str
    event: str
    path: str
    old_path: str | None = None


def snapshot_changes(device_id, before, after):
    removed, added = set(before) - set(after), set(after) - set(before)
    events = []
    # A rename is trusted only when inode identity is unique on both sides.
    def identities(items):
        result = {}
        for path, entry in items.items():
            identity = tuple(entry.revision.split(':')[:2])
            result.setdefault(identity, []).append(path)
        return result
    old_ids, new_ids = identities(before), identities(after)
    for old in sorted(removed):
        identity = tuple(before[old].revision.split(':')[:2])
        candidates = new_ids.get(identity, [])
        if len(old_ids[identity]) == len(candidates) == 1 and candidates[0] in added:
            new = candidates[0]
            events.append(FileChangeEvent(device_id, 'moved', new, old))
            added.remove(new)
        else:
            events.append(FileChangeEvent(device_id, 'deleted', old))
    events.extend(FileChangeEvent(device_id, 'created', path) for path in sorted(added))
    for path in sorted(set(before) & set(after)):
        a, b = before[path], after[path]
        if a.revision != b.revision:
            kind = 'metadata' if (a.size, a.mtime) == (b.size, b.mtime) and (a.mode, a.owner) != (b.mode, b.owner) else 'modified'
            events.append(FileChangeEvent(device_id, kind, path))
    return events



def directory_snapshot(backend, path):
    if hasattr(backend, 'watch_snapshot'):
        return backend.watch_snapshot(path)
    info = backend.transfer_properties(path)
    if info is None:
        return {}
    if info.kind != 'd':
        raise ValueError('Watching requires a real directory; symlinks are not followed')
    return {entry.path: entry for entry in backend.list_directory(path)}


@dataclass
class WatchState:
    key: tuple
    subscriptions: set = field(default_factory=set)
    monitor: object = None
    handler: int | None = None
    hint: object = None
    snapshot: dict | None = None
    busy: bool = False
    interval: float = 3.0
    due: float = 0
    generation: int = -1
    backend: object = None
    hint_attempted: bool = False


class WatchHandle:
    def __init__(self, service, identity):
        self.service, self.identity = service, identity

    def close(self):
        if self.service:
            self.service.unwatch(self.identity)
            self.service = None


class FileWatchService:
    def __init__(self, sources, resolve_device, *, remote_min=3, remote_max=15):
        self.sources, self.resolve_device = sources, resolve_device
        self.work = FileWork(sources, max_workers=2)
        self.remote_min, self.remote_max = remote_min, remote_max
        self.states, self.callbacks = {}, {}
        self.observers = {}
        self.closed, self.next_id = False, 0
        self.timer = sources.timeout(250, self.tick)

    def watch(self, location, callback, *, recursive=False):
        if self.closed:
            raise RuntimeError('File watcher stopped')
        if recursive:
            raise ValueError('Recursive watching is not exposed; subscribe to relevant directories')
        if not isinstance(location, FileLocation) or not callable(callback):
            raise ValueError('Expected FileLocation and event callback')
        key = location.device_id, location.path
        if key not in self.states and len(self.states) >= 256:
            raise ValueError('256 watched-directory limit reached')
        self.next_id += 1
        self.callbacks[self.next_id] = callback
        state = self.states.setdefault(key, WatchState(key, interval=self.remote_min))
        state.subscriptions.add(self.next_id)
        try: self._start(state)
        except Exception:
            self.unwatch(self.next_id)
            raise
        return WatchHandle(self, self.next_id)

    def _start(self, state):
        device = self.resolve_device(state.key[0])
        if device.type == 'local' and state.monitor is None:
            # GIO uses its native Linux inotify implementation. Keep monitor
            # ownership/signals on the main thread and cancel before GTK exits.
            if posixpath.isabs(state.key[1]):
                import os
                if os.path.islink(state.key[1]):
                    raise ValueError('Watching symlink directories is not supported')
            try: monitor = Gio.File.new_for_path(state.key[1]).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error as error: raise OSError(str(error)) from error
            monitor.set_rate_limit(150)
            state.monitor = monitor
            state.handler = monitor.connect('changed', self._local_changed, state)

    def _local_changed(self, monitor, file, other, kind, state):
        if self.closed or self.states.get(state.key) is not state:
            return
        mapping = {Gio.FileMonitorEvent.CREATED: 'created', Gio.FileMonitorEvent.DELETED: 'deleted',
                   Gio.FileMonitorEvent.CHANGED: 'modified', Gio.FileMonitorEvent.ATTRIBUTE_CHANGED: 'metadata',
                   Gio.FileMonitorEvent.CHANGES_DONE_HINT: 'modified', Gio.FileMonitorEvent.MOVED_IN: 'created',
                   Gio.FileMonitorEvent.MOVED_OUT: 'deleted', Gio.FileMonitorEvent.RENAMED: 'moved',
                   Gio.FileMonitorEvent.MOVED: 'moved'}
        action, path = mapping.get(kind), file.get_path()
        if action and path:
            new = other.get_path() if other and action == 'moved' else path
            self._emit(state, FileChangeEvent(state.key[0], action, new or path, path if action == 'moved' else None))

    def _emit(self, state, event):
        for identity in tuple(state.subscriptions):
            callback = self.callbacks.get(identity)
            if callback and not self.closed:
                callback(event)
        for callback in tuple(self.observers.values()):
            if not self.closed: callback(event)

    def observe(self, callback):
        if self.closed: raise RuntimeError('File watcher stopped')
        self.next_id += 1
        self.observers[self.next_id] = callback
        return WatchHandle(self, self.next_id)

    def tick(self):
        self.timer = None
        if self.closed:
            return
        now = time.monotonic()
        for state in list(self.states.values()):
            device = self.resolve_device(state.key[0])
            if device.type == 'local':
                continue
            backend = device.filesystem_backend
            if not device.connected:
                self._stop_hint(state)
                state.snapshot, state.generation = None, -1
                continue
            generation = getattr(backend, 'generation', 0)
            if state.generation != generation:
                self._stop_hint(state)
                state.snapshot, state.generation, state.due = None, generation, 0
            if state.hint and state.hint.changed():
                state.due = min(state.due, now + .3)
            if state.busy or state.due > now:
                continue
            state.busy, state.backend = True, backend
            path = state.key[1]
            # Only four remote hint channels per device. Remaining directories
            # use adaptive snapshots; no recursive whole-server walks.
            hints = sum(item.hint_attempted for item in self.states.values() if item.key[0] == state.key[0])
            try_hint = not state.hint_attempted and hints < 4 and hasattr(backend, 'watch_hint')
            state.hint_attempted = True
            def scan(backend=backend, path=path, try_hint=try_hint):
                snapshot = directory_snapshot(backend, path)
                hint = backend.watch_hint(path) if try_hint else None
                return snapshot, hint
            def done(result, error, state=state, generation=generation, backend=backend):
                state.busy = False
                if (self.closed or self.states.get(state.key) is not state
                        or generation != getattr(backend, 'generation', 0) or not backend.connected):
                    if result and result[1]: result[1].close()
                    return
                if error:
                    state.due = time.monotonic() + self.remote_max
                    return
                snapshot, hint = result
                if hint: state.hint = hint
                changes = snapshot_changes(state.key[0], state.snapshot, snapshot) if state.snapshot is not None else []
                state.snapshot = snapshot
                state.interval = self.remote_min if changes else min(self.remote_max, state.interval * 1.5)
                state.due = time.monotonic() + state.interval
                for event in changes: self._emit(state, event)
            self.work.submit(scan, done)
        self.timer = self.sources.timeout(250, self.tick)

    def _stop_hint(self, state):
        if state.hint: state.hint.close()
        state.hint, state.hint_attempted = None, False

    def unwatch(self, identity):
        self.callbacks.pop(identity, None)
        self.observers.pop(identity, None)
        for key, state in list(self.states.items()):
            state.subscriptions.discard(identity)
            if not state.subscriptions:
                self.states.pop(key)
                if state.monitor:
                    state.monitor.disconnect(state.handler)
                    state.monitor.cancel()
                self._stop_hint(state)

    def remove_device(self, device_id):
        for state in list(self.states.values()):
            if state.key[0] == device_id:
                for identity in list(state.subscriptions):
                    self.unwatch(identity)

    def close(self):
        if self.closed: return
        self.closed = True
        self.sources.remove(self.timer)
        for identity in list(self.callbacks): self.unwatch(identity)
        self.observers.clear()
        self.work.stop()
        self.resolve_device = None

    def join(self):
        self.work.join()

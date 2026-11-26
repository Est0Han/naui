"""Core, device-aware COPY jobs with bounded streams and cooperative pause.

No widget or credential crosses this boundary. Workers publish plain snapshots
and conflict requests, polled/throttled by the frontend. Directories are walked
one entry at a time; symlinks and special files are skipped, never followed.
Completed files survive cancellation; an incomplete file never replaces a target.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
import posixpath
import os
import threading
import time
import uuid

from .backends import ConflictError, DisconnectedError
from .transfer_io import CHUNK_SIZE
from .transfer_context import CURRENT_TRANSFER

FINAL_STATES = {'completed', 'failed', 'cancelled'}


@dataclass(frozen=True)
class FileLocation:
    device_id: str
    path: str

    def __post_init__(self):
        if (not isinstance(self.device_id, str) or not self.device_id or
                not isinstance(self.path, str) or not self.path.startswith('/') or
                '\0' in self.path or len(self.path.encode()) > 4096):
            raise ValueError('Transfer locations require a device ID and absolute UTF-8 path')
        object.__setattr__(self, 'path', posixpath.normpath(self.path))


class TransferCancelled(Exception):
    pass


@dataclass
class Transfer:
    id: str
    source: FileLocation
    destination: FileLocation
    state: str = 'queued'
    size: int = 0
    bytes_transferred: int = 0
    files_completed: int = 0
    skipped: int = 0
    total_known: bool = False
    speed: float = 0
    error: str = ''
    started: float = 0


@dataclass(frozen=True)
class TransferConflict:
    job_id: str
    source: FileLocation
    destination: FileLocation
    source_size: int
    destination_size: int
    directory: bool
    choices: tuple


class TransferControl:
    def __init__(self, manager, job, devices):
        self.manager, self.job, self.devices = manager, job, devices
        self.condition = threading.Condition(manager._lock)
        self.cancelled = False
        self.paused = False
        self.answer = None
        self.conflict = None

    def check(self, *, ignore_pause=False):
        with self.condition:
            waiting = time.monotonic()
            while self.paused and not ignore_pause and not self.cancelled and not self.manager.closed:
                self.condition.wait(0.1)
            if self.cancelled or self.manager.closed:
                raise TransferCancelled('Transfer cancelled')
            for backend, generation in self.devices:
                if generation != getattr(backend, 'generation', 0):
                    raise DisconnectedError('Transfer connection changed; retry on the new connection')
            return time.monotonic() - waiting

    def waiting_auth(self, waiting):
        with self.condition:
            if self.job.state not in FINAL_STATES:
                self.job.state = 'waiting_auth' if waiting else 'paused' if self.paused else 'running'


class TransferManager:
    def __init__(self, resolve_device, *, workers=2):
        self.resolve_device = resolve_device
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='naui-transfer')
        self._lock = threading.RLock()
        self.jobs, self.controls = {}, {}
        self.closed = False

    def copy(self, source, destination):
        if not isinstance(source, FileLocation) or not isinstance(destination, FileLocation):
            raise ValueError('Expected FileLocation objects')
        with self._lock:
            if self.closed:
                raise RuntimeError('Transfers have stopped')
            source_device, destination_device = self.resolve_device(source.device_id), self.resolve_device(destination.device_id)
        source_path, destination_path = source.path, destination.path
        if source_device.type == destination_device.type == 'local':
            source_path, destination_path = os.path.realpath(source.path), os.path.realpath(destination.path)
        same_ssh_endpoint = (source_device.type == destination_device.type == 'ssh' and
                             (source_device.hostname, source_device.username, source_device.port) ==
                             (destination_device.hostname, destination_device.username, destination_device.port))
        if (source.device_id == destination.device_id or same_ssh_endpoint or source_device.type == destination_device.type == 'local') and (destination_path == source_path or
                destination_path.startswith(source_path.rstrip('/') + '/')):
            raise ValueError('Cannot copy a path onto itself or into its own directory')
        with self._lock:
            if self.closed:
                raise RuntimeError('Transfers have stopped')
            if len(self.jobs) >= 512:
                self.clear_finished(keep=384)
            if sum(job.state not in FINAL_STATES for job in self.jobs.values()) >= 128:
                raise ValueError('Transfer queue is full')
            identity = uuid.uuid4().hex
            job = self.jobs[identity] = Transfer(identity, source, destination)
            source_backend, target_backend = source_device.filesystem_backend, destination_device.filesystem_backend
            control = self.controls[identity] = TransferControl(self, job,
                [(backend, getattr(backend, 'generation', 0)) for backend in (source_backend, target_backend)])
            self.executor.submit(self._run, job, control, source_backend, target_backend)
        return identity

    def move(self, *args, **kwargs):
        raise NotImplementedError('Move is not supported; copy preserves the source')

    def _run(self, job, control, source, destination):
        token = CURRENT_TRANSFER.set(control)
        try:
            control.check()
            with self._lock:
                job.state, job.started = 'running', time.monotonic()
            info = source.properties(job.source.path)
            self._copy_path(job, control, source, destination, job.source.path, job.destination.path, info, 0)
            control.check()
            with self._lock:
                job.total_known, job.state = True, 'completed'
        except TransferCancelled:
            with self._lock:
                job.state = 'cancelled'
        except Exception as error:
            with self._lock:
                job.state, job.error = 'failed', str(error)[:4096]
        finally:
            CURRENT_TRANSFER.reset(token)
            with self._lock:
                control.conflict = None

    def _copy_path(self, job, control, source, destination, src, dst, info, depth):
        control.check()
        if depth > 128:
            raise ValueError('Directory transfer exceeds depth 128')
        if info.kind not in {'f', 'd'}:
            with self._lock:
                job.skipped += 1
            return
        try:
            existing = destination.transfer_properties(dst)
        except FileNotFoundError:
            existing = None
        # RemoteFileBackend.properties reports absent entries as FileError;
        # transfer_properties distinguishes absence from permission failures.
        if existing:
            choice = self._conflict(job, control, src, dst, info, existing)
            if choice == 'keep_both':
                dst = destination.available_destination(dst)
                existing = None
            elif choice == 'replace' and info.kind == existing.kind == 'f':
                pass
            elif choice == 'merge' and info.kind == existing.kind == 'd':
                pass
            else:
                raise TransferCancelled('Transfer cancelled at conflict')
        if info.kind == 'd':
            if not existing:
                destination.create(dst, directory=True)
            iterator = source.iter_directory(src, control, info)
            try:
                for child in iterator:
                    control.check()
                    if posixpath.dirname(child.path) != src or child.name in {'', '.', '..'} or '/' in child.name:
                        raise ValueError('Transfer listing escaped its source directory')
                    self._copy_path(job, control, source, destination, child.path,
                                    posixpath.join(dst, child.name), child, depth + 1)
            finally:
                if hasattr(iterator, 'close'):
                    iterator.close()
            return
        with self._lock:
            job.size += info.size
            if depth == 0:
                job.total_known = True
        reader, writer = None, None
        try:
            reader = source.transfer_reader(src, info, control)
            writer = destination.transfer_writer(dst, info, existing.revision if existing else 'missing', control)
            remaining = info.size
            while remaining:
                control.check()
                data = reader.read(min(CHUNK_SIZE, remaining))
                if not data:
                    raise ValueError('Source ended before the advertised byte count')
                if len(data) > min(CHUNK_SIZE, remaining):
                    raise ValueError('Source exceeded the requested bounded chunk')
                writer.write(data)  # Blocking backpressure, never enqueue whole files.
                remaining -= len(data)
                with self._lock:
                    job.bytes_transferred += len(data)
                    job.speed = job.bytes_transferred / max(0.001, time.monotonic() - job.started)
            if reader.read(1):
                raise ConflictError('Source grew while transferring')
            reader.finish()
            control.check()
            writer.commit()
            with self._lock:
                job.files_completed += 1
        finally:
            if writer:
                writer.close()
            if reader:
                reader.close()

    def _conflict(self, job, control, src, dst, info, existing):
        choices = ('cancel', 'keep_both')
        if info.kind == existing.kind == 'f':
            choices += ('replace',)
        if info.kind == existing.kind == 'd':
            choices += ('merge',)
        request = TransferConflict(job.id, FileLocation(job.source.device_id, src),
            FileLocation(job.destination.device_id, dst), info.size, existing.size, info.kind == 'd', choices)
        with control.condition:
            control.answer = None
            control.conflict = request
            job.state = 'paused'
            while control.answer is None:
                control.check(ignore_pause=True)
                control.condition.wait(0.1)
            answer = control.answer
            control.conflict = None
            job.state = 'paused' if control.paused else 'running'
            return answer

    def conflicts(self):
        with self._lock:
            return [control.conflict for control in self.controls.values() if control.conflict]

    def resolve_conflict(self, identity, choice):
        with self._lock:
            control = self.controls[identity]
            if not control.conflict or choice not in control.conflict.choices:
                raise ValueError('Invalid or expired transfer conflict choice')
            control.answer = choice
            control.condition.notify_all()

    def status(self, identity=None):
        with self._lock:
            def snapshot(job):
                result = asdict(job)
                result['paused'] = self.controls[job.id].paused
                result['eta'] = ((job.size - job.bytes_transferred) / job.speed
                                 if job.total_known and job.speed > 0 else None)
                return result
            return snapshot(self.jobs[identity]) if identity else [snapshot(job) for job in self.jobs.values()]

    def clear_finished(self, keep=0):
        with self._lock:
            finished = [identity for identity, job in self.jobs.items() if job.state in FINAL_STATES]
            for identity in finished[:max(0, len(finished) - keep)]:
                self.jobs.pop(identity, None)
                self.controls.pop(identity, None)

    def active(self):
        with self._lock:
            return sum(job.state not in FINAL_STATES for job in self.jobs.values())

    def cancel(self, identity):
        with self._lock:
            control = self.controls[identity]
            control.cancelled = True
            control.condition.notify_all()

    def cancel_device(self, device_id):
        with self._lock:
            for job in self.jobs.values():
                if job.source.device_id == device_id or job.destination.device_id == device_id:
                    self.cancel(job.id)

    def pause(self, identity):
        with self._lock:
            control = self.controls[identity]
            if control.job.state not in FINAL_STATES:
                control.paused, control.job.state = True, 'paused'

    def resume(self, identity):
        with self._lock:
            control = self.controls[identity]
            control.paused = False
            if control.job.state not in FINAL_STATES:
                control.job.state = 'running'
            control.condition.notify_all()

    def retry(self, identity):
        with self._lock:
            job = self.jobs[identity]
            if job.state not in {'failed', 'cancelled'}:
                raise ValueError('Only failed or cancelled transfers can retry')
            return self.copy(job.source, job.destination)

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.resolve_device = None  # Release any frontend/window captured by the resolver.
            for identity in self.controls:
                self.cancel(identity)
        self.executor.shutdown(wait=False, cancel_futures=False)

    def join(self):
        self.executor.shutdown(wait=True)

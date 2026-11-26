"""Plain-data preview sources and private LOCAL remote-media cache ownership."""
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import threading
from .cache_sessions import mark_cache, cleanup_stale_caches

DEFAULT_REMOTE_LIMIT = 256 * 1024 * 1024
DEFAULT_CACHE_LIMIT = 512 * 1024 * 1024


@dataclass(frozen=True)
class FileProbe:
    path: str
    sample: bytes
    exists: bool = True


@dataclass(frozen=True)
class PreviewSource:
    local_path: str
    cached: bool = False


class PreviewCache:
    def __init__(self, base=None, limit=DEFAULT_REMOTE_LIMIT, cache_limit=DEFAULT_CACHE_LIMIT):
        # XDG directory variables must be absolute. Ignore malformed relative
        # values rather than dropping remote media into the current project.
        configured = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        if not configured.is_absolute():
            configured = Path.home() / ".cache"
        self.base = Path(base) if base else configured / "naui"
        self.limit = limit
        self.cache_limit = cache_limit
        self._reserved = {}
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._directory = None
        self._closed = False

    def prepare(self, backend, path, remote=False):
        if self._cancelled.is_set():
            raise OSError("Preview cancelled")
        if not remote:
            # Probe opens nonblocking and rejects devices/FIFOs before a codec
            # sees the filename. Text read/write semantics are kept separate.
            probe = backend.probe(path)
            if not probe.exists:
                raise FileNotFoundError(path)
            return PreviewSource(probe.path)
        direct = getattr(backend, 'preview_path', lambda path: None)(path)
        if direct:
            return PreviewSource(direct)  # On-demand SSHFS read; never delete it.
        with self._lock:
            if self._cancelled.is_set():
                raise OSError("Preview cancelled")
            if self._directory is None:
                self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
                cleanup_stale_caches(self.base, 'preview')
                self._directory = tempfile.TemporaryDirectory(prefix="preview-", dir=self.base)
                mark_cache(self._directory.name, 'preview')
            # Reserve the entire per-file allowance before starting a download.
            # Concurrent downloads cannot race through the aggregate cap.
            if sum(self._reserved.values()) + self.limit > self.cache_limit:
                raise OSError('Remote preview cache is full; close a preview before opening another')
            suffix = Path(path).suffix.lower()
            if not suffix[1:].isalnum() or len(suffix) > 16:
                suffix = ".bin"
            fd, destination = tempfile.mkstemp(prefix="media-", suffix=suffix, dir=self._directory.name)
            os.close(fd)
            self._reserved[destination] = self.limit
        try:
            backend.copy_to(path, destination, self.limit)
            if self._cancelled.is_set():
                raise OSError("Preview cancelled")
            with self._lock:
                self._reserved[destination] = Path(destination).stat().st_size
            return PreviewSource(destination, cached=True)
        except BaseException:
            self.release(PreviewSource(destination, cached=True))
            raise

    def release(self, source):
        if not source or not source.cached:
            return  # Never remove a user's local source file.
        with self._lock:
            if self._directory is None:
                return
            candidate = Path(source.local_path)
            if candidate.parent != Path(self._directory.name):
                raise ValueError("Cache path is outside this application's private directory")
            candidate.unlink(missing_ok=True)
            self._reserved.pop(str(candidate), None)

    def cancel(self):
        self._cancelled.set()

    def close(self):
        # Call after the window joins file workers AND players clear their
        # files. Pending/completed-undelivered downloads are covered as well.
        self.cancel()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._directory:
                self._directory.cleanup()
            self._reserved.clear()

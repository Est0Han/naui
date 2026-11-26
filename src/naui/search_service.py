"""Cancellable, bounded workspace search. Workers own data/processes, never GTK."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import posixpath
import queue
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .backends import DisconnectedError, FileError
from .commands import validate_command
from .processes import stop_process
from .search_model import SearchResult, allowed, file_score, glob_matches, parse_rg, preview, rg_arguments
from .transfer_io import RemoteStream

# EOF from our exact SSH channel stops only the command this wrapper launched.
# Remote stdin is held open while running; no remote pkill/killall is used.
REMOTE_SEARCH = r'''
cd -- "$1" || exit
shift
exec 3<&0
"$@" </dev/null & child=$!
( while IFS= read -r line <&3; do :; done; kill -TERM "$child" 2>/dev/null ) & guard=$!
trap 'kill -TERM "$child" "$guard" 2>/dev/null; wait "$child" "$guard" 2>/dev/null' EXIT
trap 'exit 130' HUP INT TERM
wait "$child"; code=$?
# rg/check-ignore exit 1 means no matches, not a failed search.
[ "$code" -eq 1 ] && code=0
exit "$code"
'''


class SearchCancelled(RuntimeError): pass
class SearchLimit(Exception): pass


class FilePathCache:
    """Small, short-lived metadata cache; never persists names or file contents."""
    def __init__(self):
        self.lock, self.values, self.generation = threading.Lock(), {}, 0

    @staticmethod
    def key(root, query):
        return (root.device_id, root.path, id(root.backend), getattr(root.backend,'generation',0),
                query.include_patterns, query.exclude_patterns, query.include_hidden, query.include_ignored)

    def get(self, key):
        with self.lock:
            entry = self.values.get(key)
            return entry[1] if entry and time.monotonic() - entry[0] < 5 else None

    def put(self, key, entries, generation):
        if len(entries) > 20000: return
        with self.lock:
            if generation != self.generation: return  # watcher invalidated during enumeration
            if sum(len(value[1]) for value in self.values.values()) + len(entries) > 40000: self.values.clear()
            self.values[key] = time.monotonic(), tuple(entries)

    def invalidate(self):
        with self.lock: self.values.clear(); self.generation += 1


class SearchSession:
    def __init__(self, identity, query):
        self.id, self.query = identity, query
        self.state = 'pending'
        self.cancelled = False
        self.condition = threading.Condition()
        self.events = queue.Queue(maxsize=512)
        self.count = 0
        self.entries = 0
        self.warnings = []
        self.limited = False
        self.future = None
        self.deadline = time.monotonic() + 300

    def check(self):
        if self.cancelled: raise SearchCancelled()
        if time.monotonic() > self.deadline: raise FileError('Search timed out; refine the query or scope.')
        return 0  # compatible with existing authentication/binary stream control

    def cancel(self):
        if self.state in {'completed', 'failed', 'cancelled'}: return
        with self.condition:
            self.cancelled = True; self.state = 'cancelled'
            self.condition.notify_all()
        if self.future: self.future.cancel()

    close = cancel

    def waiting_auth(self, waiting):
        self.auth_waiting = waiting

    def emit(self, kind, value):
        while True:
            self.check()
            try: self.events.put((kind, value), timeout=0.05); return
            except queue.Full: continue

    def result(self, value):
        self.check()
        if self.count >= self.query.max_results:
            self.limited = True
            raise SearchLimit()
        self.count += 1
        self.emit('result', value)

    def warning(self, message):
        if message not in self.warnings and len(self.warnings) < 12:
            self.warnings.append(message); self.emit('warning', message)

    def drain(self, maximum=200):
        values = []
        for _ in range(maximum):
            try: values.append(self.events.get_nowait())
            except queue.Empty: break
        return values


class LocalStream:
    def __init__(self, argv, cwd, session, stdin=None):
        self.session, self.closed = session, False
        self.errors = bytearray()
        self.process = subprocess.Popen(argv, cwd=cwd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))
        self.pending = [self.process.stdout, self.process.stderr]
        self.deadline = time.monotonic() + 300

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def read(self, size):
        while self.pending:
            self.session.check()
            if time.monotonic() > self.deadline: raise FileError('Search timed out; refine the search scope.')
            ready, _, _ = select.select(self.pending, [], [], 0.05)
            for pipe in ready:
                data = os.read(pipe.fileno(), size)
                if not data: self.pending.remove(pipe)
                elif pipe is self.process.stdout: return data
                else:
                    self.errors.extend(data); del self.errors[:-8192]
        return b''

    def finish(self):
        while self.process.poll() is None:
            self.session.check(); time.sleep(0.01)
        if self.process.returncode not in {0, 1}:
            raise FileError(bytes(self.errors).decode('utf-8', 'replace').strip() or 'Search process failed.')

    def close(self):
        if self.closed: return
        self.closed = True
        stop_process(self.process, grace=0.3)
        self.process.stdout.close(); self.process.stderr.close()


class SearchBackend:
    """Remote commands stay remote; compatibility traversal uses FileBackend."""
    def __init__(self, root):
        self.root, self.git_available, self.file_cache = root, None, None

    @contextmanager
    def stream(self, argv, session, stdin=None):
        argv = validate_command(tuple(argv), self.root.path)
        if self.root.remote:
            backend = self.root.backend
            with backend._lock:
                delegate = getattr(backend, '_delegate', backend)
            if delegate is None or not delegate.connected: raise DisconnectedError('Search device disconnected.')
            stream = RemoteStream(delegate, REMOTE_SEARCH, [self.root.path, *argv], session, control_input=True)
        else:
            stream = LocalStream(argv, self.root.path, session, stdin)
        try: yield stream
        finally: stream.close()

    def records(self, argv, session, separator=b'\n', max_record=1024*1024):
        with self.stream(argv, session) as stream:
            pending = bytearray()
            while True:
                data = stream.read(65536)
                if not data: break
                pending.extend(data)
                while separator in pending:
                    end = pending.index(separator)
                    if end > max_record: raise FileError('Search output line exceeds the preview limit; refine the search.')
                    yield bytes(pending[:end]); del pending[:end + len(separator)]
                if len(pending) > max_record: raise FileError('Search output line exceeds the preview limit; refine the search.')
            if pending: yield bytes(pending)
            stream.finish()
            errors = bytes(stream.errors).decode('utf-8', 'replace').strip()
            if errors: session.warning('Some paths could not be searched: ' + errors[:400])

    def has_rg(self, session):
        if not self.root.remote: return shutil.which('rg') is not None
        values = list(self.records(('sh', '-c', 'if command -v rg >/dev/null 2>&1; then printf rg; else printf none; fi'), session))
        return values == [b'rg']

    def ignored(self, entries, session):
        if session.query.include_ignored or self.git_available is False: return set()
        ignored = set()
        for offset in range(0, len(entries), 180):
            paths = [item.path for item in entries[offset:offset + 180]]
            if not paths: continue
            try:
                code = r'''printf '%s\0' "$@" | git check-ignore --no-index --stdin -z'''
                for value in self.records(('sh', '-c', code, 'naui', *paths), session, b'\0', 16384):
                    ignored.add(value.decode('utf-8'))
            except FileError as error:
                self.git_available = False
                message = str(error)
                if 'not a git repository' not in message.lower():
                    session.warning('Git ignore rules unavailable in compatibility search; explicit filters still apply.')
                break
        return ignored

    def entries(self, session):
        """Prune before descent; never follow symlinks or enumerate outside roots."""
        stack = [self.root.path]
        def batch(entries):
            ignored = self.ignored(entries, session)
            for entry in entries:
                if entry.path in ignored: continue
                if entry.kind == 'd':
                    if len(stack) >= 10000:
                        session.limited = True; raise SearchLimit()
                    stack.append(entry.path)
                yield entry
        while stack:
            session.check(); parent = stack.pop()
            if self.root.remote and not self.root.backend.connected: raise DisconnectedError('Search device disconnected.')
            entries = []
            try:
                for entry in self.root.backend.iter_directory(parent, session):
                    session.entries += 1
                    if session.entries > session.query.max_entries:
                        session.limited = True; raise SearchLimit()
                    relative = posixpath.relpath(entry.path, self.root.path)
                    if entry.is_symlink or entry.kind not in {'f', 'd'} or not allowed(relative, session.query, entry.kind == 'd'): continue
                    entries.append(entry)
                    if len(entries) >= 180:
                        yield from batch(entries); entries = []
                yield from batch(entries)
            except (OSError, UnicodeError) as error:
                if isinstance(error, DisconnectedError): raise
                session.warning('Some paths could not be read in ' + self.root.label + ': ' + str(error)[:250])

    def files(self, session):
        # Metadata-only traversal includes empty folders, which rg --files omits.
        if self.root.remote and not self.root.backend.connected:
            raise DisconnectedError('Search device disconnected.')
        cache = self.file_cache
        key = cache.key(self.root, session.query) if cache else None
        cached = cache.get(key) if cache else None
        generation = cache.generation if cache else 0
        found = []
        for entry in cached if cached is not None else self.entries(session):
            session.check()
            if cached is None and len(found) <= 20000: found.append(entry)
            relative = posixpath.relpath(entry.path, self.root.path)
            if entry.kind == 'd' and session.query.include_patterns and not any(
                    glob_matches(relative, pattern)
                    for pattern in session.query.include_patterns): continue
            score = file_score(session.query.text, relative, session.query.case_sensitive)
            if score is not None:
                session.result(SearchResult(self.root.device_id, self.root.path, entry.path, score=score, directory=entry.kind == 'd'))
        if cache and cached is None: cache.put(key, found, generation)

    def content(self, session, ripgrep):
        query = session.query
        if ripgrep:
            for record in self.records(rg_arguments(query), session):
                try:
                    for result in parse_rg(record, self.root): session.result(result)
                except UnicodeError: continue  # filenames/data not representable in the editor
            return
        session.warning(('Remote ' if self.root.remote else '') + 'ripgrep unavailable; using bounded compatibility search.')
        pattern = re.compile((r'(?<!\w)' if query.whole_word else '') + re.escape(query.text) + (r'(?!\w)' if query.whole_word else ''),
                             0 if query.case_sensitive else re.IGNORECASE) if not query.regex else None
        for entry in self.entries(session):
            if entry.kind != 'f' or entry.size > query.max_file_size: continue
            try:
                # At most one file is spooled. Small files stay in memory; temporary
                # storage is bounded and immediately released (no workspace mirror).
                with tempfile.SpooledTemporaryFile(max_size=256*1024) as snapshot:
                    reader = self.root.backend.transfer_reader(entry.path, entry, session)
                    received = 0
                    try:
                        while True:
                            data = reader.read(65536)
                            if not data: break
                            received += len(data)
                            if received > query.max_file_size or b'\0' in data: break
                            snapshot.write(data)
                        if data: continue
                        reader.finish()
                    finally: reader.close()
                    snapshot.seek(0)
                    # Invalid UTF-8 is skipped consistently, not silently replaced.
                    text = snapshot.read().decode('utf-8'); snapshot.seek(0)
                    if query.regex:
                        settings = json.dumps([query.text, query.case_sensitive, query.whole_word])
                        # Source installs may run with a relative PYTHONPATH. Use
                        # an absolute helper path, independent of the workspace cwd.
                        argv = (sys.executable, '-B', os.path.join(os.path.dirname(__file__), 'search_matcher.py'), settings)
                        with LocalStream(argv, self.root.path if not self.root.remote else None, session, snapshot) as stream:
                            pending = bytearray()
                            while True:
                                data = stream.read(65536)
                                if not data: break
                                pending.extend(data)
                                while b'\n' in pending:
                                    end = pending.index(10); number, start, finish, snippet = json.loads(pending[:end]); del pending[:end+1]
                                    session.result(SearchResult(self.root.device_id, self.root.path, entry.path, number, start, start, finish, snippet))
                            stream.finish()
                    else:
                        for number, line in enumerate(text.splitlines(), 1):
                            session.check()
                            for match in pattern.finditer(line):
                                start, end = match.span()
                                session.result(SearchResult(self.root.device_id, self.root.path, entry.path, number, start, start, end, preview(line, start, end)))
            except (OSError, UnicodeError) as error:
                if isinstance(error, DisconnectedError): raise
                if not isinstance(error, UnicodeError): session.warning('Some files could not be searched: ' + str(error)[:250])


class WorkspaceSearchService:
    def __init__(self, backend_factory=SearchBackend):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='naui-search')
        self.backend_factory, self.current = backend_factory, None
        self.closed, self.next_id = False, 0
        self.capabilities = {}  # bounded by configured workspace devices/session generations
        self.file_cache = FilePathCache()

    def start(self, query):
        if self.closed: raise RuntimeError('Search service closed.')
        self.cancel(); self.next_id += 1
        session = self.current = SearchSession(self.next_id, query)
        session.future = self.executor.submit(self._run, session)
        return session

    def _run(self, session):
        outcome = 'failed'
        try:
            session.check(); session.state = 'running'
            for root in session.query.roots:
                session.check(); session.emit('progress', root.label or root.device_id)
                try:
                    backend = self.backend_factory(root)
                    backend.file_cache = self.file_cache
                    if session.query.mode == 'files': backend.files(session)
                    else:
                        key = (root.device_id, id(root.backend), getattr(root.backend, 'generation', 0))
                        if key not in self.capabilities:
                            self.capabilities[key] = backend.has_rg(session)
                            if len(self.capabilities) > 128: self.capabilities = {key: self.capabilities[key]}
                        backend.content(session, self.capabilities[key])
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    session.warning((root.label or root.device_id) + ': ' + str(error)[:500])
            session.check(); outcome = 'completed'
        except SearchLimit:
            outcome = 'completed'
        except SearchCancelled:
            outcome = 'cancelled'
        except Exception as error:
            # Reporting must not call check() again after a deadline/cancellation,
            # or a failed worker could leave the UI permanently "Searching".
            message = 'Search failed: ' + str(error)[:500]
            if not session.cancelled:
                if len(session.warnings) < 12: session.warnings.append(message)
                try: session.events.put_nowait(('warning',message))
                except queue.Full: pass
        finally:
            with session.condition:
                session.state = 'cancelled' if session.cancelled else outcome

    def cancel(self):
        if self.current: self.current.cancel()

    def cancel_device(self, identity):
        self.file_cache.invalidate()
        if self.current and any(root.device_id == identity for root in self.current.query.roots):
            self.cancel()  # other roots/results remain in the UI, marked incomplete

    def close(self):
        if self.closed: return
        self.closed = True; self.cancel()
        self.file_cache.invalidate()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def join(self): self.executor.shutdown(wait=True, cancel_futures=True)

"""Pyright sessions and bounded remote source mirrors; independent of GTK.

All server processes and caches are local. Only backend reads use OpenSSH.
Remote project configs/environments/scripts are not copied or executed. URI
mapping is permanently tied to the originating device, never the selected tab.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import posixpath
import shlex
import sys
import tempfile
import threading
import time

from ...code_navigation import UriMapper, apply_text_edits, edit_groups, file_uri, within
from ...cache_sessions import mark_cache, cleanup_stale_caches
from .transport import LspClient, LspError
from ..python_lsp import pyright_command

SKIP_DIRECTORIES = {".git", ".hg", ".svn", ".ssh", ".gnupg", ".aws", ".venv", "venv", "env", "node_modules", "__pycache__", "build", "dist"}


def project_root(path, roots=(), *, remote=False):
    """Nearest local project marker inside a pin; remote discovery uses the pin.

    With no pin, the document's directory is the project. Never infer a remote
    project from THIS computer's matching pathname.
    """
    matches = [root for root in roots if within(path, root)]
    boundary = max(matches, key=len) if matches else posixpath.dirname(path)
    if not remote:
        directory = posixpath.dirname(path)
        for _ in range(32):
            if any(Path(directory, marker).exists() for marker in ("pyproject.toml", "pyrightconfig.json", ".git", "setup.cfg")):
                return directory
            if directory == boundary:
                break
            parent = posixpath.dirname(directory)
            if not within(parent, boundary) or parent == directory:
                break
            directory = parent
    return boundary


class SourceMirror:
    def __init__(self, backend, root, base=None, *, files=256, byte_limit=16 * 1024 * 1024):
        self.backend, self.root = backend, root
        configured = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        if not configured.is_absolute():
            configured = Path.home() / ".cache"
        self.base = Path(base) if base else configured / "naui"
        self.files, self.byte_limit = files, byte_limit
        self.cancelled = threading.Event()
        self.directory = None
        self.warning = ""
        self.paths = set()
        self._sizes = {}

    def write_source(self, path, data):
        """Enforce the total cache cap across refreshes and editor saves."""
        previous = self._sizes.get(path, 0)
        if (sum(self._sizes.values()) - previous + len(data) > self.byte_limit
                or path not in self._sizes and len(self._sizes) >= self.files):
            self.warning = 'Remote analysis is partial: total source cache limit reached'
            return False
        mapping = UriMapper('mirror', self.root, self.directory.name)
        target = Path(mapping.analysis_path(path))
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o600)
        self._sizes[path] = len(data)
        self.paths.add(path)
        return True

    def prepare(self, open_paths=()):
        if self.cancelled.is_set():
            raise LspError("Source mirror cancelled")
        if self.directory is None:
            self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
            cleanup_stale_caches(self.base, 'code')
            self.directory = tempfile.TemporaryDirectory(prefix="code-", dir=self.base)
            mark_cache(self.directory.name, 'code')
        mapping = UriMapper("mirror", self.root, self.directory.name)
        stack, count, size, visited = [(self.root, 0)], 0, 0, 0
        deadline = time.monotonic() + 60
        changed = []
        seen = set(open_paths)
        self.warning = ""
        while stack:
            if self.cancelled.is_set():
                raise LspError("Source mirror cancelled")
            if count >= self.files or visited >= 128 or time.monotonic() > deadline:
                self.warning = "Remote analysis is partial: mirror capped at 256 source files, 16 MiB, 128 directories and 60 seconds."
                break
            directory, depth = stack.pop()
            entries = self.backend.list_directory(directory)
            visited += 1
            for entry in entries:
                if self.cancelled.is_set():
                    raise LspError("Source mirror cancelled")
                # A malicious listing must not smuggle an unrelated path into
                # this mirror or cause traversal through a remote symlink.
                if posixpath.dirname(entry.path) != directory or not within(entry.path, self.root):
                    raise ValueError("Remote listing escaped the code project")
                if entry.kind == "d" and entry.name not in SKIP_DIRECTORIES:
                    if depth < 8:
                        stack.append((entry.path, depth + 1))
                    else:
                        self.warning = "Remote analysis is partial: directory depth is limited to 8."
                elif entry.kind == "f" and entry.name.endswith((".py", ".pyi")):
                    if count >= self.files or entry.size > 1024 * 1024 or size + entry.size > self.byte_limit:
                        self.warning = "Remote analysis is partial: source mirror size/file limits reached."
                        continue
                    snapshot = self.backend.read(entry.path)
                    data = snapshot.content.encode("utf-8")
                    if size + len(data) > self.byte_limit:
                        self.warning = "Remote analysis is partial: source mirror exceeds 16 MiB."
                        continue
                    count, size = count + 1, size + len(data)
                    seen.add(entry.path)
                    if entry.path not in open_paths:
                        if self.write_source(entry.path, data):
                            changed.append((file_uri(mapping.analysis_path(entry.path)), 2))
        if not self.warning:
            for path in self.paths - seen:
                target = Path(mapping.analysis_path(path))
                target.unlink(missing_ok=True)
                self._sizes.pop(path, None)
                changed.append((file_uri(str(target)), 3))
            self.paths = seen
        else:
            self.paths.update(seen)  # A partial scan cannot prove deletion.
        return changed

    def close(self):
        self.cancelled.set()
        if self.directory:
            self.directory.cleanup()


@dataclass(frozen=True)
class RenameFile:
    location: object
    original: str
    content: str
    snapshot: object = None
    version: int | None = None
    edits: int = 0


class LspSession:
    def __init__(self, device_id, backend, root, *, remote=False, command=None, cache_base=None, command_factory=None, settings=None):
        self.device_id, self.backend, self.remote = device_id, backend, remote
        self.root = posixpath.normpath(root) if remote else os.path.realpath(root)
        self.command, self.cache_base = command, cache_base
        self.command_factory, self.settings = command_factory, settings
        self.mapper = None
        self.mirror = SourceMirror(backend, self.root, cache_base) if remote else None
        self.client = None
        self._operations, self._client_lock = threading.RLock(), threading.Lock()
        self._cancelled = threading.Event()
        self.documents = {}  # plain path -> (version, text), no native owners.
        self._closed_versions = {}
        self.error = None
        self._python_runtime = None

    def _isolated_python(self):
        """Metadata probes must not import sitecustomize from remote sources.

        Pyright probes sys.path with python -c in the workspace directory.
        -I removes that directory and PYTHONPATH before Python site startup.
        Use this computer's interpreter, never a mounted remote executable.
        """
        if self._python_runtime is None:
            self._python_runtime = tempfile.TemporaryDirectory(prefix='naui-lsp-python-')
            wrapper = Path(self._python_runtime.name) / 'python'
            wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(os.path.realpath(sys.executable))
                               + ' -I "$@"\n', encoding='utf-8')
            wrapper.chmod(0o700)
        return str(Path(self._python_runtime.name) / 'python')

    def _public_text(self, value):
        text = self.mapper.public_text(value) if self.mapper else str(value)
        if self._python_runtime:
            text = text.replace(self._python_runtime.name, 'local Python metadata interpreter')
        return text

    @property
    def warning(self):
        return self.mirror.warning if self.mirror else ""

    def _ensure(self):
        if self._cancelled.is_set():
            raise LspError("Code analysis stopped")
        if self.error:
            raise LspError(self.error)
        if self.client:
            return
        try:
            command = self.command or (self.command_factory or pyright_command)()
            mounted = getattr(self.backend, 'local_path', lambda path: None)(self.root) if self.remote else None
            if mounted:
                if self.mirror:
                    self.mirror.close()
                    self.mirror = None
                analysis_root = mounted
            elif self.remote:
                if self.mirror is None:
                    self.mirror = SourceMirror(self.backend, self.root, self.cache_base)
                self.mirror.prepare()
                analysis_root = self.mirror.directory.name
            else:
                analysis_root = self.root
            self.mapper = UriMapper(self.device_id, self.root, analysis_root if self.remote else None)
            # Legacy direct sessions default to Pyright; registered language
            # providers supply their own command/settings without core changes.
            settings = self.settings if self.settings is not None else {"python": {"analysis": {"diagnosticMode": "openFilesOnly", "typeCheckingMode": "basic"}}}
            if self.remote:
                import copy
                settings = copy.deepcopy(settings)
                settings.setdefault('python', {})['pythonPath'] = self._isolated_python()
            if mounted:
                # No project enumeration by Naui. Limit diagnostics/indexing;
                # leave environment discovery (.venv) to the language server.
                import copy
                settings = copy.deepcopy(settings)
                analysis = settings.setdefault('python', {}).setdefault('analysis', {})
                analysis['diagnosticMode'] = 'openFilesOnly'
                analysis['indexing'] = False  # Honored by Pylance-compatible servers; optional for Pyright.
                analysis['autoImportCompletions'] = False
                analysis.setdefault('exclude', ['**/.git', '**/node_modules', '**/build', '**/dist',
                                                '**/cache', '**/logs', '**/datasets', '**/backups'])
            client = LspClient(command, self._python_runtime.name if self.remote else analysis_root, settings)
            client.display_text = self._public_text
            with self._client_lock:
                if self._cancelled.is_set():
                    raise LspError("Code analysis stopped")
                self.client = client
            client.start(file_uri(analysis_root))
            if self._cancelled.is_set():
                client.close()
                raise LspError("Code analysis stopped")
        except (OSError, ValueError, LspError) as error:
            self.error = self._public_text(error)
            raise LspError(self.error) from error

    def _sync(self, path, text, version):
        if version <= self._closed_versions.get(path, -1):
            raise LspError("Document closed; ignoring stale analysis work")
        self._ensure()
        existing = self.documents.get(path)
        if existing and existing[0] > version:
            raise LspError("Document changed; retry code navigation")
        uri = self.mapper.uri(path)
        if existing is None:
            self.client.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": version, "text": text}})
        elif existing != (version, text):
            self.client.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]})
        self.documents[path] = (version, text)
        return uri

    def sync(self, path, text, version):
        with self._operations:
            self._sync(path, text, version)
            return self.warning

    def query(self, method, path, text, version, position, **extra):
        with self._operations:
            uri = self._sync(path, text, version)
            capability = {"definition": "definitionProvider", "references": "referencesProvider", "hover": "hoverProvider", "rename": "renameProvider", "completion": "completionProvider", "formatting": "documentFormattingProvider", "codeAction": "codeActionProvider"}[method]
            if capability not in self.client.capabilities or self.client.capabilities[capability] is False:
                raise LspError("Language server does not support " + method)
            parameters = {"textDocument": {"uri": uri}, **extra}
            if method not in ("formatting", "codeAction"): parameters["position"] = position
            result = self.client.request("textDocument/" + method, parameters)
            if method == 'rename' and result is None:
                # Pyright can temporarily classify an imported declaration as
                # library code before initial workspace discovery has finished.
                # A null result is legal LSP, never an editable WorkspaceEdit.
                # Give discovery a bounded chance; don't retry protocol errors.
                deadline = time.monotonic() + 1
                while result is None and time.monotonic() < deadline:
                    if self._cancelled.wait(.1):
                        raise LspError('Code analysis stopped')
                    result = self.client.request('textDocument/rename',
                                                 {'textDocument':{'uri':uri}, 'position':position, **extra})
                if result is None:
                    raise LspError('Language server cannot rename this symbol. Reindex if project discovery is still running.')
            if method == 'hover' and isinstance(result, dict):
                def public(value):
                    if isinstance(value, str): return self.mapper.public_text(value)
                    if isinstance(value, list): return [public(item) for item in value]
                    if isinstance(value, dict): return {key: public(item) for key, item in value.items()}
                    return value
                result = {**result, 'contents': public(result.get('contents'))}
            return result

    def saved(self, path, text, version, saved_text=None):
        with self._operations:
            uri = self._sync(path, text, version)
            if self.mirror:
                self.mirror.write_source(path, (text if saved_text is None else saved_text).encode('utf-8'))
            self.client.notify("textDocument/didSave", {"textDocument": {"uri": uri}})

    def detach(self, path, version):
        with self._operations:
            if self._cancelled.is_set():
                return
            self._closed_versions[path] = max(version, self._closed_versions.get(path, -1))
            if not self.client or path not in self.documents or self.documents[path][0] > version:
                return
            self.documents.pop(path)
            self.client.notify("textDocument/didClose", {"textDocument": {"uri": self.mapper.uri(path)}})

    def refresh(self):
        with self._operations:
            if self._cancelled.is_set():
                raise LspError("Code analysis stopped")
            if self.error or self.client and self.client.failure:
                if self.client:
                    self.client.close()
                self.client, self.error = None, None
                self.documents.clear()
            self._ensure()
            if self.mirror:
                changed = self.mirror.prepare(self.documents)
                self.client.notify("workspace/didChangeWatchedFiles", {"changes": [{"uri": uri, "type": kind} for uri, kind in changed]})
            return self.warning

    def rename(self, path, text, version, position, new_name, buffers):
        with self._operations:
            # Synchronize the captured versions of ALL open Python documents
            # in this project before requesting a multi-document rename.
            for opened_path, (opened_version, opened_text) in buffers.items():
                if within(opened_path, self.root) and opened_path.endswith((".py", ".pyi")):
                    self._sync(opened_path, opened_text, opened_version)
            result = self.query("rename", path, text, version, position, newName=new_name)
            return self.rename_plan(result, buffers)

    def rename_plan(self, edit, buffers):
        """Plan all edits first. No disk writes, no GTK, no partial application."""
        with self._operations:
            if self._cancelled.is_set():
                raise LspError("Code analysis stopped")
            plan = []
            total = 0
            for location, expected, edits in edit_groups(edit, self.mapper):
                opened = buffers.get(location.path)
                if opened:
                    version, original = opened
                    if expected is not None and expected != version:
                        raise LspError("Rename response is stale; a document changed")
                    snapshot = None
                else:
                    if expected is not None:
                        raise LspError("Rename refers to an unknown document version")
                    snapshot = self.backend.read(location.path)
                    if snapshot.revision == "missing":
                        raise LspError("Rename refers to a missing file")
                    if snapshot.path != location.path:
                        raise LspError("Rename through a symbolic-link alias is not supported")
                    if self.mirror:
                        analyzed = Path(self.mapper.analysis_path(location.path)).read_text(encoding="utf-8")
                        if analyzed != snapshot.content:
                            raise LspError("Remote source changed since analysis. Reindex and retry rename.")
                    version, original = None, snapshot.content
                content = apply_text_edits(original, edits)
                total += len(content.encode())
                if total > 32 * 1024 * 1024:
                    raise LspError("Rename exceeds the 32 MiB total edit limit")
                plan.append(RenameFile(location, original, content, snapshot, version, len(edits)))
            return plan

    def cancel(self):
        self._cancelled.set()
        if self.mirror:
            self.mirror.cancelled.set()
        with self._client_lock:
            client = self.client
        if client:
            client.close()

    def close(self):
        # Call after frontend file workers have joined.
        self.cancel()
        # Device removal can close this session on a core worker while another
        # analysis job is unwinding. Serialize cache cleanup with its writes.
        with self._operations:
            if self.mirror:
                self.mirror.close()
            if self._python_runtime:
                self._python_runtime.cleanup()

"""Static API-boundary checks and explicit trusted-code subprocess validation.

This is not a sandbox/malware scanner. Importing and testing executes Python,
so the GUI must review permissions/trust before requesting execute=True.
"""
import ast
from dataclasses import dataclass, field
import importlib.util
import importlib.machinery
import importlib.abc
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid

from .plugin_manifest import capabilities, fingerprint, inspect_manifest, plugin_files

METHOD_PERMISSIONS = {
    'processes.json_lines': {'command_execution'},
    'devices.command_workspace': {'command_execution', 'remote_access'},
    'add_command': {'commands'}, 'commands.add': {'commands'}, 'commands.register': {'commands'}, 'message': set(), 'notifications.show': set(),
    'selected_location': {'filesystem_read'}, 'workspace.selected_location': {'filesystem_read'},
    'devices': {'filesystem_read'}, 'devices.list': {'filesystem_read'}, 'devices.selected': {'filesystem_read'},
    'current_document': {'editor_read'}, 'editor.current': {'editor_read'},
    'read_text': {'filesystem_read'}, 'files.read': {'filesystem_read'}, 'files.properties': {'filesystem_read'},
    'files.snapshot': {'filesystem_read'}, 'files.write': {'filesystem_write'},
    'properties': {'filesystem_read'}, 'exists': {'filesystem_read'}, 'files.exists': {'filesystem_read'},
    'create': {'filesystem_write'}, 'files.create': {'filesystem_write'},
    'open': {'filesystem_read', 'editor_navigation'}, 'editor.open': {'filesystem_read', 'editor_navigation'},
    'editor.add_action': {'commands', 'editor_read'},
    'editor.invoke': {'commands', 'editor_read', 'filesystem_write'},
    'files.watch': {'filesystem_read', 'filesystem_watch'}, 'files.observe': {'filesystem_read', 'filesystem_watch'},
    'transfers.copy': {'file_transfer', 'filesystem_read', 'filesystem_write'},
    'transfers.status': {'file_transfer'}, 'transfers.cancel': {'file_transfer'},
    'terminals.run': {'terminal_execution'}, 'terminals.interrupt': {'terminal_execution'},
    'commands.execute': {'command_execution'}, 'on_workspace_changed': {'filesystem_read'},
    'add_file_action': {'commands', 'filesystem_read'}, 'files.add_action': {'commands', 'filesystem_read'},
    'sidebar': {'views'}, 'ui.sidebar': {'views'}, 'open_text': {'views'}, 'ui.open_text': {'views'},
    'open_browser': {'views'}, 'ui.open_browser': {'views'},
    'open_diff': {'views'}, 'ui.open_diff': {'views'}, 'ui.current_diff': {'views'},
    'ui.panel': {'views'}, 'ui.focus_panel': {'views'}, 'ui.remove_panel': {'views'},
    'ui.copy_text': {'views'}, 'ui.focus_sidebar': {'views'}, 'ui.set_sidebar_badge': {'views'},
    'files.probe': {'filesystem_read'}, 'files.remove': {'filesystem_write'}, 'files.reveal': {'filesystem_read', 'editor_navigation'},
    'network.request': {'network'}, 'ui.input': {'commands'}, 'ui.confirm': {'commands'},
}


@dataclass
class ValidationResult:
    checks: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    fingerprint: str = ''
    executed: bool = False

    @property
    def ok(self): return not self.errors

    def text(self):
        lines = [('✓ ' + item) for item in self.checks]
        lines += [('Warning: ' + item) for item in self.warnings]
        lines += [('ERROR: ' + item) for item in self.errors]
        lines.append('Plugin structure and API validation passed.' if self.ok else 'Plugin validation failed.')
        if not self.executed: lines.append('Static inspection only; plugin code/tests have not been executed.')
        lines.append('Validation is not a sandbox or a malware scan. Trust the code before enabling.')
        return '\n'.join(lines)


def _api_chain(node):
    if isinstance(node, ast.Name) and node.id == 'api': return ''
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == 'api': return ''
        prefix = _api_chain(node.value)
        if prefix is not None: return (prefix + '.' if prefix else '') + node.attr
    return None


def static_validate(directory):
    result = ValidationResult()
    try:
        spec, entry, warnings = inspect_manifest(directory)
        result.checks += ['Manifest valid', 'API version supported', 'Entrypoint found', 'Permissions valid']
        result.warnings += warnings
        root = entry.parent if spec.entrypoint == 'plugin.py' else Path(directory).resolve()
        for path in plugin_files(root):
            if path.suffix != '.py': continue
            if path.stat().st_size > 1024 * 1024: raise ValueError('Python file exceeds 1 MiB: ' + str(path))
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                prefix = f'{path.relative_to(root)}:{getattr(node, "lineno", 1)}: '
                imports = []
                if isinstance(node, ast.Import): imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom): imports = [node.module or ''] if node.level == 0 else []
                for name in imports:
                    if name == 'naui' or name.startswith('naui.') or name.split('.')[0] in {'gi', 'ctypes'}:
                        result.errors.append(prefix + 'Forbidden private/native import: ' + name + '; use naui_plugin_api and api facades')
                    if name.split('.')[0] in {'subprocess', 'pty'}:
                        result.errors.append(prefix + 'Use api.commands.execute / api.terminals.run instead of ' + name)
                    if name.split('.')[0] in {'socket', 'requests', 'urllib', 'http', 'paho'} and 'network' not in spec.permissions:
                        result.errors.append(prefix + 'Network import requires network permission: ' + name)
                if isinstance(node, ast.Attribute):
                    chain = _api_chain(node)
                    if chain and any(part.startswith('_') for part in chain.split('.')):
                        result.errors.append(prefix + 'Forbidden private API access: api.' + chain)
                if isinstance(node, ast.Call):
                    chain = _api_chain(node.func)
                    required = METHOD_PERMISSIONS.get(chain, set())
                    if chain == 'ui.panel' and any(keyword.arg == 'on_submit' and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None) for keyword in node.keywords):
                        required = required | {'commands'}
                    missing = required - capabilities(spec.permissions)
                    if missing: result.errors.append(prefix + 'Undeclared permission for api.' + chain + ': ' + ', '.join(sorted(missing)))
                    if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == 'os' and node.func.attr in {'system', 'popen', 'execv', 'execve', 'spawnv'}):
                        result.errors.append(prefix + 'Use public command/terminal APIs instead of os.' + node.func.attr)
        tree = ast.parse(entry.read_text(encoding='utf-8'))
        lifecycle = any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in {'start', 'Plugin'} for node in tree.body)
        if not lifecycle: result.errors.append('Entrypoint must define class Plugin with activate(api), or legacy start(api)')
        if result.ok: result.checks.append('Python syntax and public API boundary checks passed')
        result.fingerprint = fingerprint(directory)
    except (ValueError, OSError, SyntaxError, UnicodeError, TypeError) as error:
        result.errors.append(str(error))
    return result


def load_module(directory, entry):
    """Fresh source compilation on every reload, including helper submodules."""
    namespace = 'naui_user_' + uuid.uuid4().hex
    package = types.ModuleType(namespace)
    package.__path__ = [str(Path(directory).resolve())]
    package._finder = FreshPluginFinder(namespace)
    sys.meta_path.insert(0, package._finder)
    sys.modules[namespace] = package
    module_spec = importlib.util.spec_from_file_location(namespace + '.entry', entry)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    try:
        exec(compile(Path(entry).read_bytes(), str(entry), 'exec'), module.__dict__)
        return module, namespace
    except BaseException:
        unload_module(namespace)
        raise


def unload_module(namespace):
    package = sys.modules.get(namespace)
    finder = getattr(package, '_finder', None)
    if finder in sys.meta_path: sys.meta_path.remove(finder)
    for name in list(sys.modules):
        if name == namespace or name.startswith(namespace + '.'): del sys.modules[name]


class FreshSourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        # Bypass same-size/same-mtime .pyc caches for fast AI edit/reload cycles.
        return compile(self.get_data(self.path), self.path, 'exec')


class FreshPluginFinder(importlib.abc.MetaPathFinder):
    def __init__(self, namespace): self.namespace = namespace
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.namespace + '.'): return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = FreshSourceLoader(fullname, spec.origin)
        return spec


class PluginValidator:
    def __init__(self):
        self._lock, self._processes, self.closed = threading.RLock(), {}, False

    def validate(self, directory, *, execute=False):
        result = static_validate(directory)
        if not result.ok or not execute: return result
        root = str(Path(directory).resolve())
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryFile() as output:
            with self._lock:
                if self.closed: result.errors.append('Validation cancelled'); return result
                try:
                    process = subprocess.Popen([sys.executable, '-B', '-m', 'naui.plugin_validation_worker', root],
                        cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=output,
                        stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as error:
                    result.errors.append('Unable to start plugin validator: ' + str(error))
                    return result
                self._processes[process] = root
            try:
                deadline = time.monotonic() + 30
                while process.poll() is None:
                    if time.monotonic() > deadline or output.seek(0, os.SEEK_END) > 1024 * 1024:
                        self._kill(process)
                        result.errors.append('Validation exceeded 30 seconds / 1 MiB output')
                        break
                    time.sleep(.05)
                process.wait()
                output.seek(0)
                details = output.read(65536).decode('utf-8', 'replace')
                result.executed = True
                if process.returncode != 0: result.errors.append('Import/load/tests failed:\n' + details)
                elif not result.errors:
                    result.checks += ['Entrypoint imports and lifecycle loads', details.strip()]
                if fingerprint(directory) != result.fingerprint: result.errors.append('Plugin changed during validation; validate again')
            finally:
                # Reap/stop any subprocesses the validator itself owns, including
                # helper children a trusted test left behind in its process group.
                self._kill(process)
                with self._lock: self._processes.pop(process, None)
        return result

    @staticmethod
    def _kill(process):
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass

    def cancel(self, directory):
        with self._lock:
            for process, root in self._processes.items():
                if root == str(Path(directory).resolve()): self._kill(process)

    def close(self):
        with self._lock:
            if self.closed: return
            self.closed = True
            for process in self._processes: self._kill(process)

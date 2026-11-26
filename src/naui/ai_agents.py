"""Core-owned provider-independent coding agents, using existing PTY sessions."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from .plugin_manifest import inspect_manifest, plugin_files, user_config, valid_id
from .plugin_storage import atomic_json

FINAL_STATES = {'completed', 'failed', 'cancelled'}
ENV_KEYS = {'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'COLORTERM',
    'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR', 'DISPLAY', 'WAYLAND_DISPLAY',
    'DBUS_SESSION_BUS_ADDRESS', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}


def agent_environment(environment=None):
    source = os.environ if environment is None else environment
    return {key: str(value) for key, value in source.items() if key in ENV_KEYS}


def terminal_prompt(data):
    data = data.replace(b'\r\n', b'\n')
    if any(byte < 32 and byte not in (9, 10) or byte == 127 for byte in data):
        raise ValueError('Prompt contains terminal control characters; remove them or use prompt-file delivery')
    return data


def executable_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\0\n\r'):
        raise ValueError('Executable must be a program name/path, not a shell command')
    result = shutil.which(value)
    if not result: raise ValueError('Executable not detected: ' + value)
    path = Path(result).absolute()
    if not path.is_file() or not os.access(path, os.X_OK): raise ValueError('Not an executable file')
    return str(path)


class AiAgentProvider:
    id, name, kind = '', '', 'cli'
    supports_model = False
    def available(self): return False
    def status(self): return 'Unavailable'
    def supported_models(self): return None
    def prepare(self, cwd, prompt_file, model=None): raise NotImplementedError


class CliProvider(AiAgentProvider):
    executable = ''
    def available(self): return shutil.which(self.executable) is not None
    def status(self): return 'Detected' if self.available() else self.name + ' not detected'
    def model_arguments(self, model):
        if model and (not self.supports_model or not isinstance(model, str) or len(model) > 128
                      or any(c in model for c in '\0\n\r')): raise ValueError('Invalid/unsupported model')
        return ['--model', model] if model else []


class CodexCliProvider(CliProvider):
    id, name, executable, supports_model = 'codex', 'Codex CLI', 'codex', True
    def prepare(self, cwd, prompt_file, model=None):
        # Interactive approvals/auth remain CLI-owned; no bypass/auto-approve.
        return ([executable_path(self.executable), '--cd', str(cwd), '--sandbox', 'workspace-write',
            '--ask-for-approval', 'on-request', '--no-alt-screen', *self.model_arguments(model),
            'Read and follow the Naui task instructions in ' + str(prompt_file)], 'prompt-file')


class ClaudeCliProvider(CliProvider):
    id, name, executable, supports_model = 'claude', 'Claude CLI', 'claude', True
    def prepare(self, cwd, prompt_file, model=None):
        return ([executable_path(self.executable), *self.model_arguments(model),
            'Read and follow the Naui task instructions in ' + str(prompt_file)], 'prompt-file')


class CustomCliProvider(CliProvider):
    id = 'custom'
    def __init__(self, config):
        if not isinstance(config, dict) or set(config) - {'name', 'executable', 'arguments', 'delivery'}:
            raise ValueError('Unknown custom agent settings')
        self.name, self.executable = config.get('name', ''), config.get('executable', '')
        self.arguments, self.delivery = config.get('arguments', []), config.get('delivery', '')
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 128: raise ValueError('Agent name required')
        if not isinstance(self.executable, str) or not self.executable: raise ValueError('Executable required')
        if (not isinstance(self.arguments, list) or len(self.arguments) > 64
                or any(not isinstance(a, str) or len(a) > 4096 or '\0' in a for a in self.arguments)):
            raise ValueError('Arguments must be a bounded argv list')
        if self.delivery not in {'stdin', 'terminal', 'prompt-file'}: raise ValueError('Unknown prompt delivery')
        if self.delivery == 'prompt-file' and not any('{prompt_file}' in a for a in self.arguments):
            raise ValueError('Prompt-file arguments must contain {prompt_file}')
        if any(word in a.lower() for a in self.arguments for word in ('api_key', 'api-key', 'password', '--token', 'token=', 'secret', 'authorization')):
            raise ValueError('Do not save credentials in custom agent arguments')
    def prepare(self, cwd, prompt_file, model=None):
        if model: raise ValueError('Custom agent uses its configured/default model')
        return ([executable_path(self.executable), *[a.replace('{prompt_file}', str(prompt_file)) for a in self.arguments]], self.delivery)


class UnconfiguredCliProvider(AiAgentProvider):
    id, name = 'custom', 'Custom CLI Agent'
    def status(self): return 'Not configured — open AI Agents settings'


@dataclass(frozen=True)
class ApiProviderConfig:
    """Future adapters resolve credential_reference via a core secure store."""
    url: str
    model: str
    credential_reference: str | None = None


class ApiAgentProvider(AiAgentProvider):
    id, name, kind = 'api', 'API Provider (planned)', 'api'
    def status(self): return 'No API task adapter implemented; credentials are not collected'
    def prepare(self, cwd, prompt_file, model=None): raise ValueError(self.status())


class AiAgentSettings:
    def __init__(self, path=None):
        self.path = Path(path) if path else user_config()/'ai-agents.json'
        self.data = {'schema': 1, 'default_provider': 'manual', 'default_model': '', 'custom': None}
        self.error = ''
        try:
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_size > 32768: raise ValueError('Invalid AI settings file')
                data = json.loads(self.path.read_text()); self.check(data); self.data = data
        except (ValueError, OSError, TypeError) as error: self.error = str(error)
    @staticmethod
    def check(data):
        if (not isinstance(data, dict) or set(data) != {'schema', 'default_provider', 'default_model', 'custom'}
                or data['schema'] != 1 or data['default_provider'] not in {'manual', 'codex', 'claude', 'custom'}
                or not isinstance(data['default_model'], str) or len(data['default_model']) > 128
                or any(c in data['default_model'] for c in '\0\r\n')): raise ValueError('Invalid safe AI settings; tokens/passwords not supported')
        if data['custom'] is not None: CustomCliProvider(data['custom'])
    def save(self, data):
        if self.error: raise ValueError('Preserve/fix invalid existing AI settings first: ' + self.error)
        self.check(data)
        if data['custom']: executable_path(data['custom']['executable'])
        if data['default_provider'] == 'custom' and not data['custom']: raise ValueError('Configure custom agent first')
        atomic_json(self.path, data); self.data = json.loads(json.dumps(data))


@dataclass(frozen=True)
class SnapshotFile:
    digest: str
    size: int
    text: str | None


def plugin_snapshot(directory):
    root, result, budget, scanned = Path(directory).resolve(), {}, 2 * 1024 * 1024, 0
    for path in plugin_files(root):
        limit = min(131072, budget)
        size, digest, text, captured = 0, hashlib.sha256(), None, bytearray()
        with path.open('rb') as stream:
            while chunk := stream.read(65536):
                scanned += len(chunk)
                if scanned > 64 * 1024 * 1024: raise ValueError('Plugin grew beyond 64 MiB during review')
                digest.update(chunk)
                size += len(chunk)
                if size <= limit: captured.extend(chunk)
                else: captured.clear()
        if size <= limit:
            try:
                text = captured.decode('utf-8')
                if '\0' in text: text = None
                else: budget -= size
            except UnicodeError: pass
        result[path.relative_to(root).as_posix()] = SnapshotFile(digest.hexdigest(), size, text)
    return result


def review_changes(directory, before):
    from .diff_model import text_diff
    after, lines, budget = plugin_snapshot(directory), [], 2 * 1024 * 1024
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old and new and (old.digest, old.size) == (new.digest, new.size): continue
        lines.append(('Created' if old is None else 'Deleted' if new is None else 'Modified') + ': ' + name)
        if (old is None or old.text is not None) and (new is None or new.text is not None):
            text = text_diff(old.text if old else '', new.text if new else '', 'before/'+name, 'after/'+name)
            if len(text.encode()) <= budget: lines.append(text); budget -= len(text.encode())
            else: lines.append('(Diff exceeds review limit)')
        else: lines.append('(Binary/large file; content diff omitted)')
    return '\n'.join(lines) or 'No plugin file changes.'


@dataclass
class AiTaskSession:
    id: str
    provider_id: str
    plugin_id: str
    cwd: str
    before: dict
    runtime: object
    state: str = 'starting'
    terminal_id: str | None = None
    process: object = None
    returncode: int | None = None
    error: str = ''
    delivery: str = ''
    prompt_sent: bool = False
    cleaned: bool = False
    terminal_stopped: bool = False
    def cleanup(self):
        # Popen exiting does not by itself prove that its PTY's foreground
        # descendants were stopped. Wait for the core terminal close/exit path.
        if self.cleaned or (self.process is not None and (not self.terminal_stopped or self.process.poll() is None)): return
        self.cleaned = True; self.runtime.cleanup()


class AiAgentManager:
    def __init__(self, plugin_directory, launch, stop, send, *, settings=None, changed=None):
        self.directory, self.launch, self.stop_terminal, self.send = Path(plugin_directory).resolve(), launch, stop, send
        self.settings, self.changed = settings or AiAgentSettings(), changed or (lambda: None)
        self.tasks, self.by_terminal, self.closed = {}, {}, False
    def providers(self):
        result = [CodexCliProvider(), ClaudeCliProvider()]
        result.append(CustomCliProvider(self.settings.data['custom']) if self.settings.data['custom'] else UnconfiguredCliProvider())
        return tuple(result + [ApiAgentProvider()])
    def provider(self, identity): return next((p for p in self.providers() if p.id == identity), None)
    def target(self, directory):
        root = Path(directory).expanduser().resolve(strict=True)
        if root.parent != self.directory or not valid_id(root.name): raise ValueError('AI tasks may edit only one managed user plugin directory')
        if (self.directory/root.name).is_symlink(): raise ValueError('AI target cannot be a symlink')
        spec, _, _ = inspect_manifest(root)
        if spec.id != root.name: raise ValueError('Plugin directory/identity mismatch')
        return root, spec
    def start_task(self, provider_id, directory, prompt, *, model=None, before=None):
        if self.closed: raise RuntimeError('AI agents stopped')
        provider = self.provider(provider_id)
        if provider is None or not provider.available(): raise ValueError(provider.status() if provider else 'Unknown AI provider')
        root, spec = self.target(directory)
        if any(t.cwd == str(root) and (t.state not in FINAL_STATES or not t.cleaned) for t in self.tasks.values()):
            raise ValueError('An AI task is already editing this plugin (or still stopping)')
        if len(self.tasks) >= 64: raise ValueError('64 AI tasks per session limit; restart after reviewing tasks')
        if not isinstance(prompt, str) or len(prompt.encode()) > 65536 or '\0' in prompt: raise ValueError('Invalid/oversized prompt')
        before = plugin_snapshot(root) if before is None else before
        runtime = tempfile.TemporaryDirectory(prefix='naui-ai-', dir=os.environ.get('XDG_RUNTIME_DIR'))
        task = AiTaskSession(uuid.uuid4().hex, provider_id, spec.id, str(root), before, runtime)
        self.tasks[task.id] = task
        try:
            prompt_file = Path(runtime.name)/'prompt.md'
            prompt_file.write_text(prompt, encoding='utf-8'); prompt_file.chmod(0o600)
            command, task.delivery = provider.prepare(root, prompt_file, model)
            if task.delivery == 'terminal': terminal_prompt(prompt.encode('utf-8'))
            # The current application may be a checkout while an older `naui`
            # is on PATH. A task-private launcher makes SDK/validation commands
            # use this build, without changing the user's installation or shell.
            bin_directory = Path(runtime.name)/'bin'
            bin_directory.mkdir(mode=0o700)
            launcher = bin_directory/'naui'
            launcher.write_text('#!' + sys.executable + '\nimport sys\n'
                'sys.dont_write_bytecode = True\n'
                'sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent.parent)) + ')\n'
                'from naui.__main__ import main\nraise SystemExit(main())\n')
            launcher.chmod(0o700)
            environment = agent_environment()
            environment['PATH'] = str(bin_directory) + os.pathsep + environment.get('PATH', os.defpath)
            launch_file = Path(runtime.name)/'launch.json'
            atomic_json(launch_file, {'command': command, 'cwd': str(root), 'prompt_file': str(prompt_file),
                'delivery': task.delivery, 'environment': environment})
            task.terminal_id, task.process = self.launch(task, [sys.executable, '-m', 'naui.ai_launch', str(launch_file)])
            self.by_terminal[task.terminal_id], task.state = task.id, 'running'
        except BaseException as error:
            task.state, task.error = 'failed', str(error); task.cleanup(); self.changed(); raise
        self.changed(); return task.id
    def send_prompt(self, identity):
        task = self.tasks[identity]
        if task.state != 'running' or task.delivery != 'terminal' or task.prompt_sent: raise ValueError('Terminal prompt is not pending')
        if task.process.poll() is not None: raise ValueError('Agent already exited')
        data = terminal_prompt((Path(task.runtime.name)/'prompt.md').read_bytes())
        self.send(task.terminal_id, b'\x1b[200~' + data + b'\x1b[201~\r')
        task.prompt_sent = True
    def stop_task(self, identity):
        task = self.tasks[identity]
        if task.state in FINAL_STATES: return
        previous, task.state = task.state, 'cancelled'
        try:
            if self.stop_terminal(task.terminal_id) is True: task.terminal_stopped = True
        except Exception as error:
            task.state, task.error = previous, 'Could not stop task: ' + str(error)
            self.changed(); raise
        task.cleanup(); self.changed()
    def terminal_finished(self, terminal_id, returncode=None, *, removed=False):
        identity = self.by_terminal.get(terminal_id)
        if identity is None: return False
        task = self.tasks[identity]
        task.terminal_stopped = True
        if task.state not in FINAL_STATES:
            task.state = 'cancelled' if removed else 'completed' if returncode == 0 else 'failed'
            task.returncode = returncode
            if task.state == 'failed': task.error = 'Agent process exited with code ' + str(returncode)
        task.cleanup()
        if removed: self.by_terminal.pop(terminal_id, None)
        if not self.closed: self.changed()
        return True
    def close(self):
        if self.closed: return
        self.closed = True
        for task in self.tasks.values():
            if task.state not in FINAL_STATES: task.state = 'cancelled'
            # A cancelled task may still await deferred tab disposal. During
            # application shutdown stop it while its widget remains parented.
            if task.process is not None and not task.terminal_stopped:
                try:
                    if self.stop_terminal(task.terminal_id) is True: task.terminal_stopped = True
                except Exception as error: task.error = 'Task cleanup failed: ' + str(error)
            task.cleanup()

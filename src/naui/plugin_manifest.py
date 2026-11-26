"""Toolkit-independent plugin metadata and bounded, code-free inspection."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

API_VERSION = 1
PERMISSIONS = frozenset({'commands', 'filesystem_read', 'filesystem_write', 'file_transfer',
                         'remote_access', 'editor_read', 'editor_navigation', 'filesystem_watch',
                         'command_execution', 'terminal_execution', 'views', 'network', 'terminal', 'ssh'})
ALIASES = {'terminal': 'terminal_execution', 'ssh': 'remote_access'}
SOURCES = {'bundled', 'local', 'git', 'zip', 'ai-created'}


def capabilities(permissions):
    return frozenset(ALIASES.get(p, p) for p in permissions)


@dataclass(frozen=True)
class PluginSpec:
    id: str
    name: str
    permissions: frozenset
    bundled: bool = False
    enabled_by_default: bool = False
    version: str = '0.1.0'
    description: str = ''
    api_version: int = API_VERSION
    author: str = ''
    source: str = 'local'
    entrypoint: str = 'plugin.py'


def valid_id(identity):
    return (isinstance(identity, str) and bool(re.fullmatch(r'[a-z][a-z0-9-]{0,63}', identity))
            and not identity.startswith(('base-', 'python-')))


def user_config():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Naui'
    root = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser()
    return (root if root.is_absolute() else Path.home()/'.config') / 'naui'


def plugin_directory():
    return user_config() / 'plugins'


def inspect_manifest(directory):
    root = Path(directory).expanduser().resolve(strict=True)
    warnings = []
    manifests = [root/name for name in ('plugin.toml', 'plugin.json') if (root/name).exists()]
    if len(manifests) != 1:
        raise ValueError('Provide exactly one plugin.toml (or legacy plugin.json)')
    manifest = manifests[0]
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 16384:
        raise ValueError('Manifest must be a regular file of at most 16 KiB')
    if manifest.suffix == '.toml':
        try:
            import tomllib
        except ImportError:
            try: import tomli as tomllib
            except ImportError as error: raise ValueError('TOML requires Python 3.11+ or tomli') from error
        data = tomllib.loads(manifest.read_text(encoding='utf-8'))
    else:
        data = json.loads(manifest.read_text(encoding='utf-8'))
        warnings.append('Legacy plugin.json: use plugin.toml for new plugins')
    if not isinstance(data, dict): raise ValueError('Manifest must be an object/table')
    allowed = {'id', 'name', 'version', 'description', 'api_version', 'entrypoint', 'author', 'source', 'permissions', 'ui'}
    for field in sorted(set(data) - allowed): warnings.append('Unknown manifest field: ' + field)
    identity = data.get('id')
    if not valid_id(identity): raise ValueError('Invalid/reserved plugin ID; use lowercase letters, digits and hyphens')
    for field, maximum in [('name', 128), ('version', 64), ('description', 8192), ('author', 256)]:
        value = data.get(field, '0.1.0' if field == 'version' else '')
        if (not isinstance(value, str) or len(value) > maximum or '\0' in value
                or (field in {'name', 'version'} and not value.strip())):
            raise ValueError('Invalid manifest ' + field)
    version = data.get('api_version')
    if type(version) is not int or version != API_VERSION:
        raise ValueError(f'Plugin requires API v{version}. This Naui supports API v{API_VERSION}')
    permissions = data.get('permissions', {})
    if isinstance(permissions, list) and manifest.suffix == '.json':
        if any(not isinstance(p, str) for p in permissions): raise ValueError('Invalid permissions')
        declared = frozenset(permissions)
    elif isinstance(permissions, dict):
        if any(type(value) is not bool for value in permissions.values()): raise ValueError('Permissions must be booleans')
        if set(permissions) - PERMISSIONS: raise ValueError('Unknown permission: ' + ', '.join(sorted(set(permissions) - PERMISSIONS)))
        declared = frozenset(key for key, value in permissions.items() if value)
    else: raise ValueError('Permissions must be a table of booleans')
    if declared - PERMISSIONS: raise ValueError('Unknown permissions: ' + ', '.join(sorted(declared - PERMISSIONS)))
    ui = data.get('ui', {})
    if not isinstance(ui, dict) or any(type(value) is not bool for value in ui.values()): raise ValueError('UI flags must be booleans')
    for field in set(ui) - {'sidebar', 'right_panel', 'command_palette', 'editor_actions'}: warnings.append('Unknown UI flag: ' + field)
    entrypoint = data.get('entrypoint', 'plugin.py')
    if not isinstance(entrypoint, str) or '\\' in entrypoint: raise ValueError('Invalid entrypoint')
    parts = PurePosixPath(entrypoint)
    if (parts.is_absolute() or '..' in parts.parts or str(parts) != entrypoint or parts.suffix != '.py'):
        raise ValueError('Entrypoint must be a relative .py path inside the plugin')
    entry = root/entrypoint
    if not entry.is_file() or entry.is_symlink() or not entry.resolve().is_relative_to(root):
        raise ValueError('Entrypoint must be a regular Python file inside the plugin')
    for parent in entry.parents:
        if parent == root: break
        if parent.is_symlink(): raise ValueError('Entrypoint directory cannot be a symlink')
    source = data.get('source', 'local')
    if not isinstance(source, str) or source not in SOURCES or source == 'bundled': raise ValueError('Invalid user plugin source')
    return PluginSpec(identity, data['name'], declared, version=data.get('version', '0.1.0'),
                      description=data.get('description', ''), author=data.get('author', ''),
                      source=source, entrypoint=entrypoint), entry, warnings


def plugin_files(root):
    """No symlinks, special files, caches or huge plugin trees; deterministic digest."""
    root = Path(root).resolve(strict=True)
    count, size = 0, 0
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in list(folders):
            path = Path(directory)/name
            if path.is_symlink(): raise ValueError('Plugin directory contains a symlink: ' + str(path.relative_to(root)))
        folders[:] = sorted(name for name in folders if name not in {'.git', '__pycache__', '.venv', 'venv', '.pytest_cache'})
        for name in sorted(files):
            path = Path(directory)/name
            if path.is_symlink() or not path.is_file(): raise ValueError('Plugin contains a symlink/special file: ' + str(path.relative_to(root)))
            if name.endswith(('.pyc', '.pyo')): continue
            count += 1; size += path.stat().st_size
            if count > 5000 or size > 64 * 1024 * 1024: raise ValueError('Plugin exceeds 5,000 files / 64 MiB')
            yield path


def fingerprint(root):
    root = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    for path in plugin_files(root):
        digest.update(path.relative_to(root).as_posix().encode()); digest.update(b'\0')
        with path.open('rb') as stream:
            while chunk := stream.read(65536): digest.update(chunk)
        digest.update(b'\0')
    return digest.hexdigest()

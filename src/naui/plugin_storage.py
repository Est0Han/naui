"""Atomic safe metadata/settings and staged Folder/ZIP user installation."""
import json
import copy
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile
from .plugin_manifest import PERMISSIONS, fingerprint, inspect_manifest, plugin_files, user_config, valid_id
from .plugin_validation import static_validate


def atomic_json(path, data):
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    if len(encoded) > 256 * 1024: raise ValueError('Plugin metadata/settings exceed 256 KiB')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('Settings file cannot be a symlink')
    fd, name = tempfile.mkstemp(prefix='.naui-plugin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class PluginStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else user_config()/'plugins'
        self.path = self.directory.parent/'plugin-state.json'
        self.data = {'schema': 1, 'plugins': {}, 'settings': {}}
        self.error = None
        try:
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_size > 256 * 1024: raise ValueError('Invalid plugin state file')
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or data.get('schema') != 1: raise ValueError('Invalid plugin state schema')
                if not isinstance(data.get('plugins'), dict) or not isinstance(data.get('settings'), dict): raise ValueError('Invalid plugin state')
                for identity, record in data['plugins'].items():
                    if not valid_id(identity) or not isinstance(record, dict): raise ValueError('Invalid plugin state ID')
                    if (type(record.get('enabled')) is not bool or not isinstance(record.get('grants'), list)
                            or any(not isinstance(p, str) or p not in PERMISSIONS for p in record['grants'])
                            or not isinstance(record.get('fingerprint'), str)):
                        raise ValueError('Invalid enabled/permission record')
                    if (any(key in record and type(record[key]) is not bool for key in ('startup_pending', 'recovery_required'))
                            or 'startup_failures' in record and (type(record['startup_failures']) is not int or record['startup_failures'] < 0)):
                        raise ValueError('Invalid plugin recovery record')
                self.data = data
        except (OSError, ValueError, TypeError) as error:
            self.error = str(error)  # Preserve invalid existing file until explicitly repaired.

    def save(self):
        if self.error: raise ValueError('Plugin state is invalid; preserve/fix it first: ' + self.error)
        atomic_json(self.path, self.data)

    def remember(self, identity, enabled, grants, digest, source='local'):
        previous = copy.deepcopy(self.data)
        self.data['plugins'][identity] = {'enabled': enabled, 'grants': sorted(grants), 'fingerprint': digest, 'source': source}
        try: self.save()
        except Exception:
            self.data = previous
            raise

    def setting(self, identity, key, default=None):
        return copy.deepcopy(self.data['settings'].get(identity, {}).get(key, default))

    def set_setting(self, identity, key, value):
        if not isinstance(key, str) or not key or len(key) > 128: raise ValueError('Invalid settings key')
        def check(item):
            if isinstance(item, dict):
                for name, entry in item.items():
                    if any(word in str(name).lower() for word in ('password', 'secret', 'token', 'private_key', 'credential')):
                        raise ValueError('Do not persist credentials in plugin settings')
                    check(entry)
            elif isinstance(item, list):
                for entry in item: check(entry)
        check({key: value})
        json.dumps(value, allow_nan=False)  # JSON-only safe metadata, no object serialization.
        previous = copy.deepcopy(self.data)
        self.data['settings'].setdefault(identity, {})[key] = copy.deepcopy(value)
        try: self.save()
        except Exception:
            self.data = previous
            raise


class InstallCandidate:
    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def __init__(self, source, *, zipped=False):
        self.temporary = tempfile.TemporaryDirectory(prefix='naui-plugin-install-')
        self.root = Path(self.temporary.name)/'plugin'
        self.root.mkdir()
        self.source = 'zip' if zipped else 'local'
        self.origin = str(Path(source).expanduser().absolute())
        try:
            if zipped:
                self._extract(source)
                manifests = list(self.root.rglob('plugin.toml')) + list(self.root.rglob('plugin.json'))
                if len(manifests) != 1: raise ValueError('ZIP must contain exactly one plugin manifest')
                self.root = manifests[0].parent
            else:
                origin = Path(source).expanduser().resolve(strict=True)
                for path in plugin_files(origin):
                    target = self.root/path.relative_to(origin)
                    target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, target)
            self.result = static_validate(self.root)
            if not self.result.ok: raise ValueError(self.result.text())
            self.spec, _, _ = inspect_manifest(self.root)
            self.digest = fingerprint(self.root)
        except BaseException:
            self.close(); raise

    def _extract(self, source):
        total, seen = 0, set()
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > 5000: raise ValueError('ZIP exceeds 5,000 entries')
            for entry in archive.infolist():
                name = entry.filename
                path = PurePosixPath(name)
                mode = (entry.external_attr >> 16) & 0xffff
                if (not name or not path.parts or '\\' in name or '\0' in name or path.is_absolute() or '..' in path.parts
                        or ':' in path.parts[0] or name in seen or stat.S_ISLNK(mode)
                        or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))):
                    raise ValueError('Unsafe ZIP path/type: ' + name)
                seen.add(name); total += entry.file_size
                if total > 64 * 1024 * 1024: raise ValueError('ZIP exceeds 64 MiB expanded')
                target = self.root/path
                if not target.resolve().is_relative_to(self.root): raise ValueError('ZIP path escapes extraction')
                if entry.is_dir(): target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as stream, target.open('xb') as output:
                        shutil.copyfileobj(stream, output, length=65536)

    def install(self, parent):
        parent = Path(parent).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent/self.spec.id
        if destination.exists() or destination.is_symlink(): raise ValueError('Plugin already installed; never overwrite it')
        if fingerprint(self.root) != self.digest: raise ValueError('Candidate changed after review')
        # Copy to a private stage on the destination filesystem, then atomically
        # publish by rename. The temporary stage is owned only by this operation.
        with tempfile.TemporaryDirectory(prefix='.naui-install-', dir=parent) as directory:
            stage = Path(directory)/self.spec.id
            shutil.copytree(self.root, stage, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            if destination.exists(): raise ValueError('Plugin install conflict')
            stage.rename(destination)
        return destination

    def close(self): self.temporary.cleanup()

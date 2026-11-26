"""User plugin discovery, reviewed state, validation and explicit reload."""
from dataclasses import dataclass
from pathlib import Path
import shutil
from .plugin_manifest import PluginSpec, capabilities, fingerprint, inspect_manifest
from .plugin_storage import PluginStore
from .plugin_validation import PluginValidator, ValidationResult


@dataclass
class PluginRecord:
    spec: PluginSpec
    directory: Path | None = None
    state: str = 'discovered'
    validation: ValidationResult | None = None
    error: str = ''
    source: str = 'local'


class PluginCatalog:
    def __init__(self, manager, directory=None, *, safe_mode=False):
        self.manager, self.store = manager, PluginStore(directory)
        self.safe_mode = safe_mode
        self.validator, self.records = PluginValidator(), {}
        manager.catalog = self
        self.discover()

    def discover(self):
        records = {}
        for identity, (spec, _) in self.manager.plugins.items():
            if spec.bundled: records[identity] = PluginRecord(spec, state='enabled', source='bundled')
            elif identity in self.manager.directories:
                root = self.manager.directories[identity]
                prior = self.records.get(identity)
                record = prior if prior and prior.directory == root else PluginRecord(spec, root, source=spec.source)
                record.state = 'enabled'
                records[identity] = record
        if self.store.directory.is_dir():
            for root in sorted(self.store.directory.iterdir()):
                if root.name.startswith('.') or not root.is_dir(): continue
                try:
                    if root.is_symlink(): raise ValueError('Plugin folder cannot be a symlink')
                    spec, _, _ = inspect_manifest(root)
                    if root.name != spec.id: raise ValueError('Installed folder name must equal manifest ID')
                    if spec.id in records and records[spec.id].directory != root:
                        raise ValueError('Duplicate/reserved plugin identity')
                    prior = self.records.get(spec.id)
                    record = prior if prior and prior.directory == root else PluginRecord(spec, root)
                    record.spec = spec
                    if spec.id in self.manager.plugins: record.state = 'enabled'
                    elif record.state == 'enabled': record.state = 'disabled'
                    record.source = self.store.data['plugins'].get(spec.id, {}).get('source', spec.source)
                    saved = self.store.data['plugins'].get(spec.id, {})
                    if spec.id not in self.manager.plugins and (saved.get('startup_pending') or saved.get('recovery_required')):
                        record.state, record.error = 'failed', 'Previous startup failed/interrupted. Disabled for recovery; review and Enable to try again.'
                    records[spec.id] = record
                except (OSError, ValueError, TypeError) as error:
                    identity = 'invalid:' + root.name
                    records[identity] = PluginRecord(PluginSpec(identity, root.name, frozenset()), root,
                                                    state='failed', error=str(error))
        self.records = records
        return tuple(records.values())

    def accept_validation(self, identity, result):
        record = self.records[identity]
        record.validation, record.error = result, '\n'.join(result.errors)
        if record.state != 'enabled': record.state = 'validated' if result.ok and result.executed else 'failed' if not result.ok else 'discovered'
        return result

    def validate(self, identity, *, execute=False):
        record = self.records[identity]
        if record.spec.bundled: raise ValueError('Bundled plugins are maintained and tested with Naui')
        return self.accept_validation(identity, self.validator.validate(record.directory, execute=execute))

    def enable(self, identity, grants, *, persist=True):
        if self.safe_mode: raise ValueError('SAFE MODE: user plugins cannot be enabled; restart normally after recovery')
        record = self.records[identity]
        if record.spec.bundled: raise ValueError('Bundled lifecycle is managed by Naui')
        result = record.validation
        if not result or not result.ok or not result.executed: raise ValueError('Import/test validation required before enabling')
        if fingerprint(record.directory) != result.fingerprint: raise ValueError('Plugin changed; validate again before enabling')
        current, _, _ = inspect_manifest(record.directory)
        if current != record.spec:
            raise ValueError('Plugin metadata changed; refresh and review permissions/identity again')
        if capabilities(record.spec.permissions) - capabilities(grants): raise PermissionError('Permissions must be explicitly reviewed')
        persist = persist and record.directory == self.store.directory/identity
        if persist:
            # Save trust only after all checks, before code loading; restore the
            # disabled state if activation fails. Failed persistence never runs code.
            self.store.remember(identity, True, grants, result.fingerprint, record.source)
        try:
            if persist: self.startup_begin(identity)
            self.manager.load_user(record.directory, grants)
            record.state, record.error = 'enabled', ''
            if persist: self.startup_finished(identity)
        except BaseException as error:
            record.state, record.error = 'failed', str(error)
            self.manager.log(identity, 'Load failed: ' + str(error))
            if persist: self.store.remember(identity, False, grants, result.fingerprint, record.source)
            raise

    def disable(self, identity, *, failed=False):
        record = self.records.get(identity)
        if not record: return
        if record.spec.bundled: return  # Essential preserved adapters remain core-managed.
        self.manager.deactivate(identity)
        record.state = 'failed' if failed else 'disabled'
        if record.directory == self.store.directory/identity:
            old = self.store.data['plugins'].get(identity, {})
            self.store.remember(identity, False, old.get('grants', []), old.get('fingerprint', ''), record.source)

    def reload(self, identity):
        record = self.records[identity]
        old = self.store.data['plugins'].get(identity, {})
        grants = old.get('grants', [])
        if record.directory != self.store.directory/identity and identity in self.manager.apis:
            grants = self.manager.apis[identity].permissions
        result = record.validation
        if (not result or not result.ok or not result.executed
                or fingerprint(record.directory) != result.fingerprint):
            raise ValueError('Validate the changed plugin before reloading')
        current, _, _ = inspect_manifest(record.directory)
        if current != record.spec:
            raise ValueError('Plugin metadata changed; refresh and review permissions/identity again')
        if capabilities(record.spec.permissions) - capabilities(grants):
            raise PermissionError('New permissions require review: Enable with the new grants')
        self.manager.deactivate(identity)
        self.enable(identity, grants)

    def uninstall(self, identity):
        record = self.records[identity]
        if record.spec.bundled or record.directory is None: raise ValueError('Cannot uninstall bundled plugins')
        expected = self.store.directory/identity
        if record.directory != expected or expected.is_symlink() or expected.parent.resolve() != self.store.directory.resolve():
            raise ValueError('Refusing to delete an unmanaged plugin directory')
        spec, _, _ = inspect_manifest(expected)
        if spec.id != identity: raise ValueError('Plugin identity changed; refusing removal')
        self.disable(identity)
        shutil.rmtree(expected)  # Only the exact confirmed user plugin; no remote operations.
        self.store.data['plugins'].pop(identity, None)
        self.store.save()
        self.records.pop(identity)

    def restore_candidates(self):
        if self.safe_mode: return []
        return [identity for identity, record in self.records.items() if not record.spec.bundled
                and self.store.data['plugins'].get(identity, {}).get('enabled')
                and not self.store.data['plugins'][identity].get('startup_pending')
                and not self.store.data['plugins'][identity].get('recovery_required')]

    def startup_begin(self, identity):
        self.store.data['plugins'][identity]['startup_pending'] = True
        self.store.save()  # Crash marker must reach disk BEFORE in-process import.

    def startup_finished(self, identity, *, failed=False):
        saved = self.store.data['plugins'][identity]
        saved['startup_pending'] = False
        saved['recovery_required'] = failed
        saved['startup_failures'] = saved.get('startup_failures', 0) + 1 if failed else 0
        self.store.save()

    def restore_check(self, identity):
        record = self.records[identity]
        saved = self.store.data['plugins'].get(identity, {})
        if fingerprint(record.directory) != saved.get('fingerprint'):
            return ValidationResult(errors=['Plugin changed since approval; review, validate and enable again'])
        if capabilities(record.spec.permissions) - capabilities(saved.get('grants', [])):
            return ValidationResult(errors=['Permissions changed since approval'])
        return self.validator.validate(record.directory, execute=True)

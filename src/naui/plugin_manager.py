"""Trusted plugin runtime: guarded callbacks, fresh loads and owned teardown."""
from collections import defaultdict, deque
from pathlib import Path
import re
from .plugin_api import PluginAPI
from .plugin_manifest import PERMISSIONS, PluginSpec, capabilities, inspect_manifest
from .plugin_validation import load_module, static_validate, unload_module


def sanitize_log(value):
    return re.sub(r'(?i)((?:password|token|secret|credential)\s*[:=]\s*)\S+', r'\1[redacted]', str(value))[:4000]


class PluginManager:
    def __init__(self, host):
        self.host = host
        from .keybindings import CommandRegistry
        self.registry = getattr(host, "command_registry", None) or CommandRegistry()
        self.features, self.commands, self.plugins, self.apis = {}, {}, {}, {}
        self.file_actions, self.shortcuts, self.listeners = {}, {}, {}
        self.modules, self.logs, self.failures = {}, defaultdict(lambda: deque(maxlen=200)), {}
        self.directories = {}
        self.catalog = None
        self.closed = False

    def log(self, identity, message):
        self.logs[identity].append(sanitize_log(message))

    def activate(self, spec, entrypoint, grants):
        if self.closed: raise RuntimeError('Plugins stopped')
        if spec.id in self.plugins: raise ValueError('Plugin ID already loaded')
        if spec.permissions - PERMISSIONS or capabilities(spec.permissions) - capabilities(grants):
            raise PermissionError('Plugin requested ungranted or unsupported permissions')
        api = PluginAPI(self.host, spec.id, capabilities(spec.permissions), bundled=spec.bundled)
        self.apis[spec.id], self.plugins[spec.id] = api, (spec, None)
        try:
            instance = entrypoint(api)
            self.plugins[spec.id] = (spec, instance)
            self.failures.pop(spec.id, None)
            self.log(spec.id, 'Enabled ' + spec.name)
        except BaseException:
            api.close()
            del self.apis[spec.id], self.plugins[spec.id]
            raise
        return api

    def load_bundled(self):
        from .plugins import bundled_plugins
        for spec, start in bundled_plugins(): self.activate(spec, start, spec.permissions)

    @staticmethod
    def inspect_user(directory):
        spec, entry, _ = inspect_manifest(directory)
        return spec, entry

    def load_user(self, directory, grants):
        spec, path = self.inspect_user(directory)
        if capabilities(spec.permissions) - capabilities(grants): raise PermissionError('Plugin requested ungranted permissions')
        if spec.id in self.plugins: raise ValueError('Plugin ID already loaded')
        result = static_validate(directory)
        if not result.ok: raise ValueError(result.text())
        module, namespace = load_module(directory, path)
        def start(api):
            if 'Plugin' in module.__dict__:
                instance = module.Plugin()
                try: instance.activate(api)
                except BaseException:
                    try: instance.deactivate()
                    except BaseException: pass
                    raise
                return instance
            return module.start(api)
        try:
            api = self.activate(spec, start, grants)
            self.modules[spec.id] = namespace
            self.directories[spec.id] = Path(directory).expanduser().resolve()
            return api
        except BaseException:
            unload_module(namespace)
            raise

    def deactivate(self, identity):
        """UI callers defer this when invoked by a plugin's own native signal."""
        pair = self.plugins.pop(identity, None)
        api = self.apis.pop(identity, None)
        if api:
            # Disable entry to every API-owned callback before plugin teardown.
            api.quiescing = True
        if pair:
            instance = pair[1]
            stop = getattr(instance, 'deactivate', None) or getattr(instance, 'stop', None)
            if stop:
                try: stop()
                except BaseException as error: self.log(identity, 'Deactivation failed: ' + str(error))
        if api: api.close()
        namespace = self.modules.pop(identity, None)
        if namespace: unload_module(namespace)
        if pair: self.log(identity, 'Unloaded')

    def fail(self, identity, error):
        if self.closed or identity in self.failures: return
        message = type(error).__name__ + ': ' + sanitize_log(error)
        self.failures[identity] = message
        self.log(identity, message)
        if identity in self.apis: self.apis[identity].quiescing = True
        if self.catalog and identity in self.catalog.records:
            record = self.catalog.records[identity]
            record.state, record.error = 'failed', message
        if not self.host._shutting_down:
            try: self.host.set_status('Plugin failed: ' + identity + ' — ' + message)
            except Exception: pass
            sources = getattr(self.host, 'sources', None)
            if sources: sources.idle(self._failed_cleanup, identity)
            else: self._failed_cleanup(identity)

    def _failed_cleanup(self, identity):
        self.deactivate(identity)
        if self.catalog:
            try: self.catalog.disable(identity, failed=True)
            except Exception as error: self.log(identity, str(error))
        self.changed()

    def changed(self):
        view = getattr(self.host, 'plugin_manager_view', None) if self.host else None
        if view and not view._shutting_down: view.refresh()

    def invoke(self, plugin_id, command_id):
        if self.closed: return
        command = self.commands.get((plugin_id, command_id))
        if command: return command[1]()

    def emit(self, event):
        if not self.closed:
            for callback in list(self.listeners.get(event, {}).values()): callback()

    def close(self):
        if self.closed: return
        self.closed = True
        if self.catalog: self.catalog.validator.close()
        for identity in reversed(list(self.plugins)): self.deactivate(identity)
        self.commands.clear(); self.file_actions.clear(); self.shortcuts.clear(); self.listeners.clear()
        self.features.clear()
        self.host = None

"""Public, capability-checked plugin facade. No SSH/authentication objects.

This is an API permission boundary, NOT a sandbox for arbitrary Python. User
plugins execute in-process and must be explicitly trusted before loading.
"""
from dataclasses import dataclass

from .transfers import FileLocation
from .plugin_manifest import PERMISSIONS, API_VERSION


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    type: str
    display_name: str
    state: str
    current_path: str | None = None
    roots: tuple = ()
    home: str | None = None
    active: bool = False


@dataclass(frozen=True)
class DocumentInfo:
    location: FileLocation
    text: str
    dirty: bool


class TransferAPI:
    def __init__(self, api):
        self._api = api
        self._owned = set()

    def copy(self, source, destination):
        self._api.require('file_transfer', 'filesystem_read', 'filesystem_write')
        self._api.location(source)
        self._api.location(destination)
        identity = self._api._host.transfers.copy(source, destination)
        self._owned.add(identity)
        if len(self._owned) > 512:
            self._owned.intersection_update(self._api._host.transfers.jobs)
        return identity

    def status(self, identity):
        self._api.require('file_transfer')
        self._check(identity)
        return self._api._host.transfers.status(identity)

    def cancel(self, identity):
        self._api.require('file_transfer')
        self._check(identity)
        self._api._host.transfers.cancel(identity)

    def _check(self, identity):
        if identity not in self._owned:
            raise PermissionError('Plugin does not own this transfer')


class PluginAPI:
    version = API_VERSION

    def __init__(self, host, plugin_id, permissions, *, bundled=False):
        self._host, self.plugin_id = host, plugin_id
        self.permissions = frozenset(permissions)
        self.bundled = bundled
        self.closed = False
        self.quiescing = False
        self.transfers = TransferAPI(self)
        self.files = FileAPI(self)
        self.terminals = TerminalAPI(self)
        self.processes = ProcessAPI(self)
        self.commands = CommandAPI(self)
        self.devices = DeviceAPI(self)
        self.editor = EditorAPI(self)
        self.ui = UIAPI(self)
        self.notifications = NotificationAPI(self)
        self.workspace = WorkspaceAPI(self)
        self.settings = SettingsAPI(self)
        self.logs = LogAPI(self)
        self.network = NetworkAPI(self)
        self._watches, self._timers = [], set()
        self._browsers = []
        self._views = []

    def require(self, *permissions):
        if self.closed or self.quiescing or self._host._shutting_down:
            raise RuntimeError('Plugin stopped')
        missing = set(permissions) - self.permissions
        if missing:
            raise PermissionError('Plugin API permission missing: ' + ', '.join(sorted(missing)))

    def guard(self, callback):
        """Every callback into a plugin has a lifetime and failure boundary."""
        def invoke(*args, **kwargs):
            if self.closed or self.quiescing or self._host._shutting_down: return None
            try: return callback(*args, **kwargs)
            except (Exception, SystemExit) as error:
                self._host.plugins.fail(self.plugin_id, error)
                return None
        return invoke

    def location(self, value):
        if not isinstance(value, FileLocation):
            raise ValueError('Expected FileLocation')
        device = self._host.workspace.devices[value.device_id]
        if device.type == 'ssh':
            self.require('remote_access')
        return device

    def selected_location(self):
        self.require('filesystem_read')
        explorer = self._host.workspace_sidebar.active_explorer
        path = explorer.selected_path()
        location = FileLocation(explorer.device.id, path) if path else None
        if location:
            self.location(location)
        return location

    def devices(self):
        self.require('filesystem_read')
        return tuple(DeviceInfo(device.id, device.type, device.display_name, device.state,
                                device.navigation_state.current_path, tuple(root.path for root in device.roots),
                                device.navigation_state.home, device.id == self._host.workspace.active_device_id)
                     for device in self._host.workspace.devices.values()
                     if device.type == 'local' or 'remote_access' in self.permissions)

    def current_document(self):
        self.require('editor_read')
        editor = self._host.current_editor()
        if editor is None or editor.loading:
            return None
        location = FileLocation(editor.device_id, editor.path)
        self.location(location)
        return DocumentInfo(location, editor.text(), editor.dirty)

    def read_text(self, location, callback):
        self.require('filesystem_read')
        device = self.location(location)
        # The public result is text, never a backend/connection credential owner.
        def complete(snapshot, error):
            if not self.closed:
                self.guard(callback)(snapshot.content if snapshot else None, error)
        self._host.work(lambda: device.filesystem_backend.read(location.path), complete)

    def properties(self, location, callback):
        self.require('filesystem_read')
        backend = self.location(location).filesystem_backend
        self._host.work(lambda: backend.properties(location.path),
                        self.guard(callback))

    def exists(self, location, callback):
        self.require('filesystem_read')
        backend = self.location(location).filesystem_backend
        self._host.work(lambda: backend.transfer_properties(location.path) is not None,
                        self.guard(callback))

    def create(self, location, *, directory=False, callback=None):
        self.require('filesystem_write')
        backend = self.location(location).filesystem_backend
        self._host.work(lambda: backend.create(location.path, directory=directory),
                        lambda value, error: self.guard(callback)(error) if callback else None)

    def open(self, location, line=0, column=0):
        self.require('filesystem_read', 'editor_navigation')
        self.location(location)
        from .code_navigation import CodeLocation, validate_position
        validate_position({'line': line, 'character': column})
        self._host.code_navigation.open_link(CodeLocation(location.device_id, location.path, line, column))

    def add_command(self, command_id, label, callback, *, default_shortcut=None, context='global', enabled=None):
        self.require('commands')
        from .keybindings import Command
        if not isinstance(command_id,str) or not command_id or not isinstance(label,str) or not callable(callback) or enabled is not None and not callable(enabled):
            raise ValueError('Invalid plugin command')
        registry = self._host.plugins.registry
        # Plugin namespaces prevent accidental replacement of core commands.
        identity = (command_id if command_id.startswith(self.plugin_id + '.') else self.plugin_id + '.' + command_id)
        if self.bundled:
            identity = {'base-runner.current':'runner.run_current', 'base-runner.stop':'runner.stop',
                        'base-git.history':'git.open_history', 'base-git.refresh':'git.refresh',
                        **{'base-git.' + key: 'git.' + key for key in ('open_source_control', 'stage', 'unstage', 'open_diff')}}.get(identity,identity)
        guarded = self.guard(callback)
        registry.register(Command(identity,label,'Plugins — '+self.plugin_id if not self.bundled else self.plugin_id.removeprefix('base-').title(),
            context,guarded,default_shortcut,self.guard(enabled) if enabled else lambda:not self.closed and not self.quiescing, self.plugin_id))
        self._host.plugins.commands[(self.plugin_id, command_id)] = (label,guarded)
        return identity

    def message(self, text):
        self.require()
        self._host.set_status(str(text)[:4000])

    def register_feature(self, name, implementation):
        """Bundled adapters bridge legacy GTK features during gradual extraction.

        Third-party plugins use the public operations above and never receive a
        window, terminal or authentication object. This factory hook is restricted
        to trusted bundled adapters rather than advertised as a sandbox bypass.
        """
        self.require()
        if not self.bundled:
            raise PermissionError('Feature-host factories are reserved for bundled adapters')
        self._host.plugins.features[name] = implementation

    def on_workspace_changed(self, callback):
        self.require('filesystem_read')
        self._host.plugins.listeners.setdefault('workspace', {})[self.plugin_id] = self.guard(callback)

    def defer(self, milliseconds, callback):
        self.require()
        if not 0 <= milliseconds <= 60000: raise ValueError('Invalid delay')
        identity = None
        def done():
            self._timers.discard(identity)
            self.guard(callback)()
        identity = self._host.sources.timeout(milliseconds, done)
        self._timers.add(identity)
        return identity

    def cancel_deferred(self, identity):
        if not self.closed and identity in self._timers:
            self._host.sources.remove(identity)
            self._timers.discard(identity)

    def add_file_action(self, identity, label, predicate, callback):
        self.require('commands', 'filesystem_read')
        self._host.plugins.file_actions[(self.plugin_id, identity)] = (label, self.guard(predicate), self.guard(callback))

    def add_shortcut(self, shortcut, callback):
        # Compatibility with API v1's original runner shortcuts. New plugins
        # register named commands with optional defaults instead.
        if shortcut not in {'Ctrl+F5','Shift+F5'}: raise ValueError('Unsupported legacy shortcut')
        self.add_command('shortcut.' + ('run' if shortcut=='Ctrl+F5' else 'stop'),
                         'Run' if shortcut=='Ctrl+F5' else 'Stop', callback,
                         default_shortcut=shortcut, context='editor' if shortcut=='Ctrl+F5' else 'global')

    def confirm(self, title, message, button, callback):
        self.require('commands')
        self._host.confirm_action(title, message, button, self.guard(callback))

    def input(self, title, fields, callback):
        self.require('commands')
        self._host.plugin_input(title, fields, self.guard(callback))

    def open_text(self, device_id, title, content, language='diff'):
        self.require('views')
        self.location(FileLocation(device_id, '/'))
        if len(content.encode()) > 8 * 1024 * 1024: raise ValueError('View exceeds 8 MiB')
        self._views = [view for view in self._views if not getattr(view, '_shutting_down', False)]
        view = self._host.open_text_view(device_id, title, content, language)
        if view is not None:
            view.keybinding_context = 'git' if self.plugin_id == 'base-git' else 'plugin'
            self._views.append(view)

    def sidebar(self, identity, title, rows):
        self.require('views')
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or any(ord(char) < 32 for char in identity):
            raise ValueError('Invalid sidebar identity')
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError('Invalid sidebar title')
        self._host.plugin_sidebar(self.plugin_id, identity, title, self._rows(rows))

    def _rows(self, rows):
        count = 0
        def convert(values, depth=0):
            nonlocal count
            if depth > 4: raise ValueError('Sidebar nesting limit reached')
            result = []
            for row in values:
                count += 1
                if count > 2000: raise ValueError('Sidebar row limit reached')
                item = dict(row)
                for key in ('actions', 'menu'):
                    if key in row:
                        if len(row[key]) > 24: raise ValueError('Sidebar action limit reached')
                        item[key] = [(label, self.guard(callback)) for label, callback in row[key]]
                if 'select' in row: item['select'] = self.guard(row['select'])
                if 'primary' in row: item['primary'] = self.guard(row['primary'])
                if 'input' in row: item['input'] = dict(row['input'], changed=self.guard(row['input']['changed']))
                if 'children' in row: item['children'] = convert(row['children'], depth + 1)
                result.append(item)
            return result
        return convert(rows)

    def open_diff(self, device_id, title, patch, *, location=None, repository=None, staged=False, historical=False, hunk_action=None):
        self.require('views')
        self.location(FileLocation(device_id, '/'))
        if hunk_action is not None:
            self.require('command_execution')
            if not callable(hunk_action): raise ValueError('Invalid diff action')
        from .diff_model import size_mode
        size_mode(patch)  # Validate text before passing it to native buffers.
        if location:
            self.require('filesystem_read', 'editor_navigation')
            self.location(location)
            if location.device_id != device_id: raise ValueError('Diff location belongs to another device')
        self._views = [view for view in self._views if not getattr(view, '_shutting_down', False)]
        view = self._host.open_diff_view(device_id, title, patch,
            open_file=self.guard(lambda: self.open(location)) if location else None,
            context={'device_id': device_id, 'repository': repository, 'location': location, 'staged': bool(staged), 'historical': bool(historical)})
        if view is not None:
            self._views.append(view)
            if hunk_action is not None:
                view.add_hunk_action(self.guard(hunk_action))

    def open_browser(self, device_id, title, refresh, more):
        """Core-owned list/filter/detail main tab; no native widgets exposed."""
        self.require('views')
        self.location(FileLocation(device_id, '/'))
        self._browsers = [browser for browser in self._browsers if not browser.closed]
        if len(self._browsers) >= 16: raise ValueError('16 plugin browser views limit reached')
        self._views = [view for view in self._views if not getattr(view, '_shutting_down', False)]
        view = self._host.open_browser_view(device_id, title)
        view.keybinding_context = 'git' if self.plugin_id == 'base-git' else 'plugin'
        handle = BrowserHandle(self, view)
        view.signals.connect(view.refresh, 'clicked', lambda *_: self.guard(refresh)() if not handle.closed else None)
        view.signals.connect(view.more, 'clicked', lambda *_: self.guard(more)() if not handle.closed else None)
        self._browsers.append(handle)
        self._views.append(view)
        return handle

    def decorate_files(self, device_id, root, branch, statuses):
        self.require('views', 'filesystem_read')
        self.location(FileLocation(device_id, root))
        self._host.workspace_sidebar.explorers[device_id].set_git_status(root, branch, statuses)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.processes.close()
        self._host.plugins.registry.unregister_owner(self.plugin_id)
        for browser in self._browsers: browser.stop()
        self._browsers.clear()
        for watch in self._watches: watch.close()
        self._watches.clear()
        for identity in self._timers: self._host.sources.remove(identity)
        self._timers.clear()
        # Core owns native widgets. UI disable/reload runs on an idle turn,
        # after originating signals unwind; global shutdown leaves GTK's normal
        # window destruction in charge rather than removing widgets twice.
        remove = getattr(self._host, 'remove_plugin_ui', None)
        if remove and not self._host._shutting_down: remove(self.plugin_id, self._views)
        self._views.clear()
        for identity in list(self.transfers._owned):
            try: self._host.transfers.cancel(identity)
            except (KeyError, RuntimeError): pass
        self.transfers._owned.clear()
        for collection in (self._host.plugins.file_actions,):
            for key in list(collection):
                if key[0] == self.plugin_id: del collection[key]
        for key, value in list(self._host.plugins.shortcuts.items()):
            if value[0] == self.plugin_id: del self._host.plugins.shortcuts[key]
        for listeners in self._host.plugins.listeners.values(): listeners.pop(self.plugin_id, None)
        for key in list(self._host.plugins.commands):
            if key[0] == self.plugin_id:
                del self._host.plugins.commands[key]
        self._host = None


class FileAPI:
    def __init__(self, api): self.api = api

    def watch(self, *, device_id, path, callback, recursive=False):
        api = self.api
        api.require('filesystem_read', 'filesystem_watch')
        location = FileLocation(device_id, path)
        api.location(location)
        api._watches = [handle for handle in api._watches if handle.service is not None]
        if len(api._watches) >= 128: raise ValueError('Plugin watch limit reached')
        watch = api._host.file_watch.watch(location, api.guard(callback),
                                           recursive=recursive)
        api._watches.append(watch)
        return watch

    def observe(self, callback):
        api = self.api
        api.require('filesystem_read', 'filesystem_watch')
        def changed(event):
            if not api.closed:
                device = api._host.workspace.devices.get(event.device_id)
                if device and (device.type == 'local' or 'remote_access' in api.permissions): api.guard(callback)(event)
        handle = api._host.file_watch.observe(changed)
        api._watches.append(handle)
        return handle

    def read(self, location, callback): return self.api.read_text(location, callback)
    def probe(self, location, callback):
        api = self.api
        api.require('filesystem_read')
        backend = api.location(location).filesystem_backend
        def done(probe, error):
            api.guard(callback)(probe.sample if probe else None, error)
        api._host.work(lambda: backend.probe(location.path), done)

    def remove(self, location, expected_revision, callback):
        api = self.api
        api.require('filesystem_write')
        backend = api.location(location).filesystem_backend
        if not isinstance(expected_revision, str) or not expected_revision or len(expected_revision) > 4096:
            raise ValueError('Expected a file revision')
        api._host.check_mutation(backend, location.path)
        def remove():
            entry = backend.properties(location.path)
            if entry.is_directory: raise ValueError('File removal refuses directories')
            backend.delete(location.path, expected_revision)
        api._host.work(remove, api.guard(callback))

    def reveal(self, location):
        api = self.api
        api.require('filesystem_read', 'editor_navigation')
        device = api.location(location)
        explorer = api._host.workspace_sidebar.explorers.get(device.id)
        if explorer:
            api._host.show_sidebar_panel('explorer')
            api._host.workspace_sidebar.cards[device.id][0].set_expanded(True)
            import posixpath
            explorer.pending_select = location.path
            parent = posixpath.dirname(location.path)
            explorer.set_context(device.filesystem_backend, explorer.session, parent, explorer.home, explorer.connection_state)
            device.navigation_state.expanded.add(parent)
            explorer._restore_state()

    def snapshot(self, location, callback):
        self.api.require('filesystem_read')
        backend = self.api.location(location).filesystem_backend
        self.api._host.work(lambda: backend.read(location.path), self.api.guard(callback))
    def write(self, location, content, expected, callback):
        self.api.require('filesystem_write')
        if not isinstance(content, str) or not isinstance(expected, str) or not expected:
            raise ValueError('UTF-8 text and an explicit original revision are required')
        backend = self.api.location(location).filesystem_backend
        self.api._host.work(lambda: backend.write(location.path, content, expected), self.api.guard(callback))
    def properties(self, location, callback): return self.api.properties(location, callback)
    def exists(self, location, callback): return self.api.exists(location, callback)
    def create(self, location, *, directory=False, callback=None): return self.api.create(location, directory=directory, callback=callback)
    def add_action(self, identity, label, predicate, callback): return self.api.add_file_action(identity, label, predicate, callback)


class BrowserHandle:
    """Presentation facade with plugin/view lifetime guards, no GTK API."""
    def __init__(self, api, view): self.api, self._view = api, view

    @property
    def closed(self): return self.api.closed or self.api.quiescing or self._view._shutting_down or self.api._host._shutting_down

    def _guard(self, callback):
        return self.api.guard(lambda *args: callback(*args) if not self.closed else None)

    def rows(self, rows):
        if self.closed: return
        if len(rows) > 1000: raise ValueError('Browser row limit exceeded')
        self._view.set_rows([dict(row, select=self._guard(row['select'])) for row in rows])

    def filters(self, filters, callback):
        if self.closed: return
        if len(filters) > 512: raise ValueError('Browser filter limit exceeded')
        self._view.set_filters(filters, self._guard(callback))

    def detail(self, text, files=()):
        if self.closed: return
        if len(text.encode()) > 8 * 1024 * 1024 or len(files) > 500: raise ValueError('Browser detail limit exceeded')
        self._view.set_detail(text, [dict(row, select=self._guard(row['select'])) for row in files])

    def state(self, message, connected=True, more=False):
        if self.closed: return
        self._view.availability(connected, message)
        self._view.more.set_sensitive(connected and more)

    def present(self):
        if not self.closed:
            notebook = self.api._host.notebook
            notebook.set_current_page(notebook.page_num(self._view))

    def stop(self):
        self._view.close()
        # Explicit browser stop can originate in a native selection signal.
        # Remove only its own page on the next turn, after callbacks unwind.
        host = self.api._host
        if not self.api.closed and not self.api.quiescing and host and not host._shutting_down:
            remove = getattr(host, 'remove_plugin_view', None)
            if remove: host.sources.idle(remove, self._view)


class CommandAPI:
    def __init__(self, api): self.api = api

    def add(self, identity, label, callback): return self.api.add_command(identity, label, callback)

    def register(self, id, title, callback, *, default_shortcut=None, context="global", enabled=None):
        return self.api.add_command(id,title,callback,default_shortcut=default_shortcut,context=context,enabled=enabled)

    def execute(self, *, device_id, cwd, command, callback, input_data=None):
        api = self.api
        api.require('command_execution')
        from .commands import validate_command
        command = validate_command(command, cwd)
        backend = api.location(FileLocation(device_id, cwd)).filesystem_backend
        generation, service = getattr(backend, 'generation', 0), api._host.command_service
        def done(result, error):
            if not api.closed:
                if generation != getattr(backend, 'generation', 0): error = RuntimeError('Device connection changed')
                api.guard(callback)(result if not error else None, error)
        api._host.work(lambda: service.execute(backend, command, cwd, **({"input_data": input_data} if input_data is not None else {})), done)


class TerminalAPI:
    def __init__(self, api): self.api, self.owned = api, set()

    def run(self, *, device_id, cwd, command, new_terminal=False):
        api = self.api
        api.require('terminal_execution')
        api.location(FileLocation(device_id, cwd))
        from .commands import validate_command
        identity = api._host.queue_run(device_id, cwd, validate_command(command, cwd), new_terminal)
        self.owned.add(identity)
        return identity

    def interrupt(self, identity):
        self.api.require('terminal_execution')
        if identity not in self.owned: raise PermissionError('Plugin does not own this terminal run')
        self.api._host.interrupt_run(identity)


class ProcessAPI:
    """Owned JSON-lines subprocesses for trusted plugins with command permission."""
    def __init__(self, api): self.api, self.clients = api, []
    def json_lines(self, *, cwd, command):
        self.api.require('command_execution')
        from pathlib import Path
        if not isinstance(cwd,str) or '\0' in cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError('Choose an existing absolute local directory')
        if not isinstance(command,(list,tuple)) or not 1<=len(command)<=128 or any(not isinstance(a,str) or '\0' in a or len(a.encode())>65536 for a in command):
            raise ValueError('Invalid subprocess command')
        self.clients = [c for c in self.clients if not c.stopped.is_set()]
        if len(self.clients)>=4: raise ValueError('Close an existing subprocess first')
        from .process_protocol import Client
        client = Client(cwd, command=command)
        self.clients.append(client)
        return client
    def close(self):
        for client in self.clients: client.close()
        self.clients.clear()


class DeviceAPI:
    def __init__(self, api): self.api = api
    def __call__(self): return PluginAPI.devices(self.api)
    def list(self): return self()
    def selected(self): return next((device for device in self() if device.active), None)
    def command_workspace(self, *, device_id, cwd, existing=None):
        self.api.require('command_execution', 'remote_access')
        device = self.api.location(FileLocation(device_id, cwd))
        if device.type != 'ssh': raise ValueError('SSH_WORKSPACE_NOT_REMOTE')
        if device.state != 'connected': raise ValueError('SSH_WORKSPACE_DISCONNECTED')
        from .ssh_workspace import prepare_workspace
        try: return prepare_workspace(self.api.plugin_id, device, cwd, existing=existing)
        except OSError as error: raise ValueError('SSH_WORKSPACE_LOCAL_STORAGE') from error
        except ValueError as error: raise ValueError('SSH_WORKSPACE_INVALID_CONTEXT') from error

    def connect(self, device_id):
        """Open the selected device's ordinary SSH connection/authentication UI."""
        self.api.require('remote_access', 'terminal_execution')
        device = self.api.location(FileLocation(device_id, '/'))
        if device.type != 'ssh': raise ValueError('Select an SSH device')
        return self.api._host.activate_device(device_id, reconnect=True)


class EditorAPI:
    def __init__(self, api): self.api = api
    def current(self): return self.api.current_document()
    def open(self, location, line=0, column=0): return self.api.open(location, line, column)
    def invoke(self, command):
        """Invoke a known, undoable line command on the active document only."""
        self.api.require('commands', 'editor_read', 'filesystem_write')
        from .editor_commands import LINE_COMMANDS
        if not isinstance(command,str) or command not in LINE_COMMANDS: raise ValueError('Unsupported public editor command')
        document = self.current()  # Enforce originating device/remote permission.
        if document is None: return False
        editor = self.api._host.current_editor()
        return editor.command(command)
    def add_action(self, identity, label, callback):
        self.api.require('editor_read', 'commands')
        self.api.add_command(identity, label, lambda: callback(self.current()))


class NotificationAPI:
    def __init__(self, api): self.api = api
    def show(self, text): return self.api.message(text)


class UIAPI:
    def __init__(self, api): self.api = api
    def supports(self, capability):
        """Feature discovery for additive v1 UI extensions."""
        self.api.require()
        return capability in {'right_panels', 'panel_text', 'panel_input', 'panel_conversation', 'panel_attachments', 'panel_message_actions', 'panel_toolbars'}

    def panel(self, identity, title, rows=(), *, text='', on_submit=None, placeholder='Message…', messages=None, accept_attachments=False, header_rows=(), composer_rows=()):
        api = self.api
        api.require('views')
        if on_submit is not None: api.require('commands')
        if type(accept_attachments) is not bool: raise ValueError('Invalid attachment flag')
        if accept_attachments: api.require('filesystem_read')
        from .plugin_panels import validate_panel
        validate_panel(identity, title, text, placeholder, on_submit, rows, messages)
        for controls in (header_rows, composer_rows):
            if not isinstance(controls,(list,tuple)) or len(controls)>8: raise ValueError('At most eight toolbar controls allowed')
            validate_panel(identity, title, '', placeholder, None, controls)
        if header_rows or composer_rows: api.require('commands')
        if messages is not None:
            if any(m.get('actions') for m in messages): api.require('commands')
            messages = [dict(m, actions=[(label, api.guard(callback)) for label, callback in m.get('actions', [])]) for m in messages]
        api._host.right_panels.publish(api.plugin_id, identity, title, api._rows(rows), text,
            api.guard(on_submit) if on_submit else None, placeholder,
            lambda: not api.closed and not api.quiescing, messages=messages, accept_attachments=accept_attachments,
            header_rows=api._rows(header_rows), composer_rows=api._rows(composer_rows))

    def focus_panel(self, identity):
        self.api.require('views')
        self.api._host.right_panels.focus_panel(self.api.plugin_id, identity)

    def remove_panel(self, identity):
        self.api.require('views')
        host, owner = self.api._host, self.api.plugin_id
        # Unparent only after an originating callback has returned to GTK.
        host.sources.idle(lambda: host.right_panels.remove(owner, identity)
                          if not self.api.closed and not host._shutting_down else None)

    def sidebar(self, identity, title, rows): return self.api.sidebar(identity, title, rows)
    def open_text(self, device_id, title, content, language=None): return self.api.open_text(device_id, title, content, language)
    def open_browser(self, device_id, title, refresh, more): return self.api.open_browser(device_id, title, refresh, more)
    def open_diff(self, device_id, title, patch, **options): return self.api.open_diff(device_id, title, patch, **options)
    def current_diff(self):
        self.api.require('views')
        host = self.api._host
        view = host.notebook.get_nth_page(host.notebook.get_current_page())
        context = getattr(view, 'diff_context', None)
        if context and context.get('device_id') and context.get('location'):
            self.api.location(FileLocation(context['device_id'], '/'))
            return dict(context, selected_hunk=view.current_hunk)
        return None
    def copy_text(self, text):
        self.api.require('views')
        if not isinstance(text, str) or len(text.encode()) > 65536: raise ValueError('Clipboard text exceeds limit')
        self.api._host.get_clipboard().set(text)
    def focus_sidebar(self, identity):
        self.api.require('views')
        section = self.api._host.sidebar_sections.get((self.api.plugin_id, identity))
        if section:
            section.set_expanded(True)
            self.api._host.show_sidebar_panel(self.api._host.plugin_panel_id(self.api.plugin_id, identity))
    def set_sidebar_badge(self, identity, count):
        self.api.require('views')
        if type(count) is not int or not 0 <= count <= 1000000: raise ValueError('Invalid sidebar badge')
        if (self.api.plugin_id, identity) not in self.api._host.sidebar_sections: raise ValueError('Register sidebar before setting its badge')
        self.api._host.plugin_sidebar_badge(self.api.plugin_id, identity, count)
    def input(self, title, fields, callback): return self.api.input(title, fields, callback)
    def confirm(self, title, message, button, callback): return self.api.confirm(title, message, button, callback)


class WorkspaceAPI:
    def __init__(self, api): self.api = api
    def selected_location(self): return self.api.selected_location()
    def selected_directory(self):
        """Selected tree folder (or a selected file's parent), with device identity."""
        self.api.require('filesystem_read')
        explorer = self.api._host.workspace_sidebar.active_explorer
        path = explorer.selected_parent()
        if not path: return None
        location = FileLocation(explorer.device.id, path)
        self.api.location(location)
        return location
    def on_changed(self, callback): return self.api.on_workspace_changed(callback)


class SettingsAPI:
    def __init__(self, api): self.api = api
    def get(self, key, default=None):
        self.api.require()
        return self.api._host.plugins.catalog.store.setting(self.api.plugin_id, key, default)
    def set(self, key, value):
        self.api.require()
        self.api._host.plugins.catalog.store.set_setting(self.api.plugin_id, key, value)


class LogAPI:
    def __init__(self, api): self.api = api
    def write(self, message):
        self.api.require()
        self.api._host.plugins.log(self.api.plugin_id, message)


class NetworkAPI:
    def __init__(self, api): self.api = api
    def request(self, url, callback, *, method='GET', data=None):
        """Bounded HTTP(S) requests off the UI thread; no credential store."""
        self.api.require('network')
        from urllib.parse import urlsplit
        from urllib.request import Request, urlopen
        if not isinstance(url, str) or urlsplit(url).scheme not in {'http', 'https'}:
            raise ValueError('Expected an HTTP(S) URL')
        if method not in {'GET', 'POST'} or (data is not None and (not isinstance(data, bytes) or len(data) > 1024 * 1024)):
            raise ValueError('Only GET/POST and at most 1 MiB request data are supported')
        def request():
            with urlopen(Request(url, data=data, method=method), timeout=10) as response:
                content = response.read(8 * 1024 * 1024 + 1)
                if len(content) > 8 * 1024 * 1024: raise ValueError('Response exceeds 8 MiB')
                return content
        self.api._host.work(request, self.api.guard(callback))

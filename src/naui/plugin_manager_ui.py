"""Core-owned main-area plugin manager; all plugin data is modelled, not widgets."""
from pathlib import Path
import weakref
from gi.repository import Gtk, Pango
from .lifecycle import Signals
from .plugin_manifest import PERMISSIONS, inspect_manifest
from .plugin_sdk import agent_prompt, scaffold
from .plugin_storage import InstallCandidate

PERMISSION_LABELS = {
    'commands': 'Register commands / dialogs', 'views': 'Add panels and read-only views',
    'terminal': 'Run programs in real terminals', 'ssh': 'Access SSH devices',
    'filesystem_read': 'Read device files', 'filesystem_write': 'Create / modify device files',
    'file_transfer': 'Transfer files between devices', 'network': 'Network access',
    'editor_read': 'Read current editor contents', 'editor_navigation': 'Open files / jump to positions',
    'filesystem_watch': 'Watch device files', 'command_execution': 'Run device commands (background)',
    'terminal_execution': 'Run programs in real terminals', 'remote_access': 'Access SSH devices',
}

BUILTIN_DESCRIPTIONS = {
    'base-file-explorer': 'Browse and manage files on your computer and SSH devices.',
    'base-filepath': 'Open file paths and jump to locations in the editor.',
    'base-lsp': 'Connect language tools for definitions, references and code diagnostics.',
    'python-lsp': 'Python code assistance using Pyright, when installed.',
    'base-media': 'Preview images, audio and video in tabs.',
    'base-runner': 'Run the current file in a terminal.',
    'base-git': 'View Git changes, diffs and history.',
}


def label(text):
    widget = Gtk.Label(label=str(text), xalign=0, wrap=True, selectable=True)
    return widget


class PluginManagerView(Gtk.Box):
    keybinding_context = "plugin"
    def __init__(self, window, *, developer=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.window, self.manager, self.catalog = window, window.plugins, window.plugins.catalog
        self._shutting_down, self.busy = False, False
        self.signals, self.row_signals = Signals(), Signals()
        self.dialogs, self.candidates = set(), []
        self.dialog_controls = {}
        self.device_id, self.path = 'local', 'Plugins'
        self.backend, self.session = window.local_backend, None
        self.disconnected, self.loading = False, False
        self.label = Gtk.Label(label='Plugins')
        self.return_page = window.notebook.get_nth_page(window.notebook.get_current_page())
        self.last_prompt, self.last_result = '', ''
        self.validation_paths = set()
        self.expanded_sections = set()
        for edge in ('start', 'end', 'top', 'bottom'):
            getattr(self, 'set_margin_' + edge)(16)
        from .ai_agent_ui import AiAgentUI
        self.agents = AiAgentUI(self)
        heading = label('Plugins')
        heading.set_selectable(False)
        heading.add_css_class('title-1')
        self.append(heading)
        self.append(label('Manage your extensions.'))
        toolbar = Gtk.Box(spacing=8)
        toolbar.append(self.toolbar_menu('Add Plugin…', [
            ('From a ZIP file…', lambda: self.choose(True)),
            ('From a folder…', lambda: self.choose(False))]))
        toolbar.append(self.toolbar_menu('Create & develop', [
            ('Create a plugin with AI…', self.create_dialog),
            ('Configure AI coding agents…', self.agents.settings),
            ('Guide to creating plugins', self.agents.guide),
            ('Copy instructions for an AI coding agent', self.copy_master),
            ('Open developer documentation', self.open_sdk),
            ('Open installed plugins folder', self.open_user_plugins)]))
        refresh = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Refresh installed plugins')
        refresh.update_property([Gtk.AccessibleProperty.LABEL], ['Refresh installed plugins'])
        self.signals.connect(refresh, 'clicked', lambda *_: self.defer(self.rediscover))
        toolbar.append(refresh)
        self.append(toolbar)
        if self.catalog.safe_mode: self.append(label('SAFE MODE — User plugins cannot load. Disable/uninstall for recovery; saved preferences remain intact.'))
        self.status = label('')
        self.signals.connect(self.status, 'notify::label', lambda widget, *_: widget.set_visible(bool(widget.get_text())))
        self.status.set_visible(False)
        self.append(self.status)
        self.listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.listbox); scroll.set_vexpand(True)
        self.append(scroll)
        self.refresh()

    def toolbar_menu(self, title, actions):
        menu = Gtk.MenuButton(label=title)
        popover = Gtk.Popover()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for text, callback in actions:
            button = Gtk.Button(label=text, has_frame=False)
            self.signals.connect(button, 'clicked', lambda *_, callback=callback: (popover.popdown(), self.defer(callback)))
            content.append(button)
        popover.set_child(content)
        menu.set_popover(popover)
        return menu

    def section(self, title, key, content):
        expander = Gtk.Expander(label=title, expanded=key in self.expanded_sections)
        expander.set_child(content)
        self.row_signals.connect(expander, 'notify::expanded', lambda widget, *_:
            self.expanded_sections.add(key) if widget.get_expanded() else self.expanded_sections.discard(key))
        return expander

    @property
    def return_page(self): return self._return_page() if self._return_page else None

    @return_page.setter
    def return_page(self, value): self._return_page = weakref.ref(value) if value is not None else None

    def defer(self, callback):
        if not self._shutting_down and not self.window._shutting_down:
            self.window.sources.idle(lambda: self.attempt(callback) if not self._shutting_down else None)

    def attempt(self, callback):
        if self._shutting_down or self.window._shutting_down: return
        try: return callback()
        except (Exception, SystemExit) as error:
            self.refresh()
            self.status.set_text('This plugin changed on disk. Click Refresh, then Enable to review the updated permissions.'
                                 if 'Plugin metadata changed' in str(error) else 'Could not complete the action: ' + str(error)[:4000])

    def button(self, box, text, action, enabled=True):
        button = Gtk.Button(label=text)
        button.set_sensitive(enabled and not self.busy)
        self.row_signals.connect(button, 'clicked', lambda *_: self.defer(action))
        box.append(button)
        return button

    def refresh(self):
        if self._shutting_down or self.window._shutting_down: return
        self.row_signals.close()
        while self.listbox.get_first_child(): self.listbox.remove(self.listbox.get_first_child())
        for bundled, heading in [(False, 'Installed by you'), (True, 'Included with NaUi')]:
            records = [record for record in self.catalog.records.values() if record.spec.bundled == bundled]
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            if bundled:
                self.listbox.append(self.section(f'{heading} ({len(records)})', 'bundled', group))
                group.append(label('These features ship with NaUi. You do not need to install them here.'))
            else:
                title = label(heading)
                title.add_css_class('heading')
                self.listbox.append(title)
                self.listbox.append(group)
            if not records:
                group.append(label('No extra plugins installed. Use Add Plugin to choose a ZIP file or folder.'))
            for record in records:
                identity, spec = record.spec.id, record.spec
                row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                row.set_margin_start(8); row.set_margin_end(8)
                header = Gtk.Box(spacing=6)
                state = ('Safe mode' if self.catalog.safe_mode and not bundled else
                         'Enabled' if identity in self.manager.plugins else
                         'Needs attention' if record.error else 'Disabled')
                title = label(spec.name)
                title.add_css_class('heading')
                title.set_hexpand(True); header.append(title)
                header.append(label(state.capitalize()))
                if not bundled:
                    valid = not identity.startswith('invalid:')
                    self.button(header, 'Disable' if identity in self.manager.plugins else 'Enable',
                        lambda identity=identity: self.toggle(identity), valid and (not self.catalog.safe_mode or identity in self.manager.plugins))
                    header.append(self.plugin_menu(identity))
                row.append(header)
                description = spec.description or BUILTIN_DESCRIPTIONS.get(identity, '')
                if description: row.append(label(description))
                if record.error:
                    notice = label('Needs attention — open Details for the error and repair options.')
                    notice.add_css_class('dim-label'); row.append(notice)
                self.button(header, 'Details', lambda identity=identity: self.details(identity))
                row.set_margin_top(8); row.set_margin_bottom(8)
                group.append(row)
        if self.window.ai_agents.tasks:
            tasks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            self.agents.render(tasks)
            self.listbox.append(self.section(f'AI activity ({len(self.window.ai_agents.tasks)})', 'ai-activity', tasks))
        commands = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        commands.append(label('Actions provided by enabled plugins. These use your previously active editor or terminal.'))
        for (identity, command), (title, _) in list(self.manager.commands.items()):
            row = Gtk.Box()
            self.button(row, title, lambda identity=identity, command=command: self.invoke(identity, command))
            commands.append(row)
        if self.manager.commands:
            self.listbox.append(self.section('Plugin commands', 'commands', commands))
        if self.catalog.store.error: self.status.set_text('Plugin state error (original file preserved): ' + self.catalog.store.error)

    def details(self, identity):
        record = self.catalog.records[identity]
        spec, validation = record.spec, record.validation
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(label(spec.description or BUILTIN_DESCRIPTIONS.get(identity, '')))
        content.append(label(f'Version {spec.version} · {record.source}'))
        if spec.author: content.append(label('By ' + spec.author))
        content.append(label('Permissions\n' + ('\n'.join(PERMISSION_LABELS.get(p, p) for p in sorted(spec.permissions)) or 'None')))
        if record.error: content.append(label('Error\n' + record.error[:2000]))
        content.append(label('Checks: ' + ('Included with NaUi' if spec.bundled else
                            'Passed' if validation and validation.ok else
                            'Failed' if validation else 'Not checked yet')))
        scroll = Gtk.ScrolledWindow(vexpand=True); scroll.set_child(content)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12); box.append(scroll)
        dialog = self.dialog(spec.name, box)
        if not dialog: return
        controls = self.dialog_controls[dialog] = Signals()
        actions = Gtk.Box(spacing=6); box.append(actions)
        callbacks = [('View logs', lambda: self.logs(identity))]
        if validation: callbacks.append(('Check results', lambda: self.text_dialog('Check results', validation.text())))
        for title, callback in callbacks:
            button = Gtk.Button(label=title)
            def clicked(*_, callback=callback):
                dialog.response(Gtk.ResponseType.CANCEL); self.defer(callback)
            controls.connect(button, 'clicked', clicked); actions.append(button)

    def plugin_menu(self, identity):
        """Native per-entry overflow; actions still defer until signals unwind."""
        record = self.catalog.records[identity]
        valid = not identity.startswith('invalid:')
        managed = valid and record.directory == self.catalog.store.directory/identity
        loaded = identity in self.manager.plugins
        menu = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text='Plugin actions')
        popover = Gtk.Popover(); content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        actions = [
            ('Reload plugin', lambda: self.reload(identity), valid and loaded and not self.catalog.safe_mode),
            ('Check plugin (runs its tests)…', lambda: self.validate(identity), valid),
            ('Edit plugin source', lambda: self.window.open_file(self.window.local_backend, str(inspect_manifest(record.directory)[1])), valid),
            ('Open Plugin Folder', lambda: self.open_folder(identity), True),
            ('Open terminal in plugin folder', lambda: self.terminal_here(identity), True),
            ('Write an AI change request…', lambda: self.modify_dialog(identity), valid),
            ('Start AI Agent', lambda: self.agents.choose(identity), managed),
            ('Uninstall', lambda: self.uninstall(identity), managed),
        ]
        if record.error:
            actions[-1:-1] = [
                ('Copy error report for AI', lambda: self.copy_error(identity), valid),
                ('Ask AI agent to fix error…', lambda: self.agents.choose(identity, repair=True), managed)]
        for title, callback, enabled in actions:
            button = Gtk.Button(label=title); button.set_sensitive(enabled and not self.busy)
            if title == 'Uninstall': button.add_css_class('destructive-action')
            def clicked(*_, callback=callback):
                popover.popdown(); self.defer(callback)
            self.row_signals.connect(button, 'clicked', clicked); content.append(button)
        popover.set_child(content); menu.set_popover(popover)
        return menu

    def rediscover(self):
        self.catalog.discover(); self.refresh()

    def dialog(self, title, content, callback=None, accept='OK', prepare=None, cancelled=None):
        if self.window.dialog: self.window.dialog.present(); return None
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.set_default_size(650, 450)
        dialog.add_button('Cancel' if callback else 'Close', Gtk.ResponseType.CANCEL)
        if callback: dialog.add_button(accept, Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.get_content_area().append(content)
        self.window.dialog = dialog; self.dialogs.add(dialog)
        def response(dialog, answer):
            # Read inputs before disposal, but defer actions/removal until the
            # native response callback has unwound. No plugin owns this widget.
            value = None
            if answer == Gtk.ResponseType.ACCEPT and prepare:
                try: value = prepare()
                except Exception as error:
                    self.status.set_text(str(error))
                    content.append(label('Error: ' + str(error)))
                    return
            self.signals.disconnect(dialog)
            controls = self.dialog_controls.pop(dialog, None)
            if controls: controls.close()
            if self.window.dialog is dialog: self.window.dialog = None
            self.dialogs.discard(dialog)
            dialog.destroy()
            if answer == Gtk.ResponseType.ACCEPT and callback:
                self.defer(lambda: callback(value) if prepare else callback())
            elif cancelled: self.defer(cancelled)
        self.signals.connect(dialog, 'response', response)
        dialog.present()
        return dialog

    def copy(self, text):
        self.get_clipboard().set(text)
        self.status.set_text('Copied to clipboard')

    def text_dialog(self, title, text, *, prompt=False, actions=()):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        scroll = Gtk.ScrolledWindow(); scroll.set_child(view); scroll.set_vexpand(True)
        box.append(scroll)
        button = Gtk.Button(label='Copy AI Prompt' if prompt else 'Copy')
        dialog = self.dialog(title, box)
        if dialog:
            # A dialog-owned handler is removed with the dialog; Signals keeps
            # no widget reference across repeated open/close cycles.
            controls = self.dialog_controls[dialog] = Signals()
            controls.connect(button, 'clicked', lambda *_: self.copy(text))
            box.append(button)
            for title, callback in actions:
                action = Gtk.Button(label=title)
                def clicked(*_, callback=callback):
                    dialog.response(Gtk.ResponseType.CANCEL)
                    self.defer(callback)
                controls.connect(action, 'clicked', clicked); box.append(action)
        return dialog

    def review(self, identity, action, callback):
        record = self.catalog.records[identity]
        body = label(record.spec.name + '\n' + str(record.directory) + '\n\nRequested permissions:\n' +
                     '\n'.join(PERMISSION_LABELS.get(p, p) for p in sorted(record.spec.permissions)) +
                     '\n\nImporting/testing/enabling executes this plugin’s Python code. Enable only code you trust. '
                     'Validation does not make third-party code safe.')
        self.dialog(action + ': ' + record.spec.name, body, callback, accept='Trust and ' + action)

    def validate(self, identity, after=None, *, reviewed=False):
        record = self.catalog.records[identity]
        if not reviewed:
            self.review(identity, 'Validate', lambda: self.validate(identity, after, reviewed=True)); return
        validator, directory = self.catalog.validator, str(record.directory)
        self.busy = True; self.validation_paths.add(directory)
        self.status.set_text('Loading/importing plugin and running tests…'); self.refresh()
        def done(result, error):
            self.validation_paths.discard(directory)
            if self._shutting_down: return
            self.busy = False
            if error: self.status.set_text(str(error)); self.refresh(); return
            self.last_result = result.text()
            self.catalog.accept_validation(identity, result)
            self.status.set_text('Plugin checks passed.' if result.ok else 'Plugin checks failed. Open Details for results.')
            self.refresh()
            if result.ok and after: self.attempt(after)
        self.window.work(lambda: validator.validate(directory, execute=True), done)

    def toggle(self, identity):
        if identity in self.manager.plugins:
            self.catalog.disable(identity); self.refresh(); return
        self.require_agent_stopped(identity)
        record = self.catalog.records[identity]
        def enable():
            self.require_agent_stopped(identity)
            self.catalog.enable(identity, record.spec.permissions); self.refresh()
        self.review(identity, 'Enable', lambda: self.validate(identity, enable, reviewed=True))

    def reload(self, identity):
        self.require_agent_stopped(identity)
        self.rediscover()
        self.review(identity, 'Reload', lambda: self.validate(identity, lambda: self._reload(identity), reviewed=True))

    def _reload(self, identity):
        self.require_agent_stopped(identity)
        record = self.catalog.records[identity]
        # The Reload review also authorizes any newly declared permissions.
        # Catalog.reload never silently expands old grants; use the explicitly
        # reviewed Enable route when permission metadata changed.
        old = self.catalog.store.data['plugins'].get(identity, {}).get('grants', [])
        from .plugin_manifest import capabilities
        if capabilities(record.spec.permissions) - capabilities(old):
            self.catalog.disable(identity)
            self.catalog.enable(identity, record.spec.permissions)
        else: self.catalog.reload(identity)
        self.refresh()

    def create_dialog(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        entries = {}
        for key, title in [('name', 'Plugin name'), ('id', 'Plugin ID (lowercase-with-hyphens)')]:
            box.append(label(title)); entry = Gtk.Entry(); box.append(entry); entries[key] = entry
        box.append(label('Description / requested behavior'))
        description = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        description.set_size_request(-1, 100); box.append(description)
        choices = {}
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3)
        for permission in sorted(PERMISSIONS - {'remote_access', 'terminal_execution'}):
            checkbox = Gtk.CheckButton(label=PERMISSION_LABELS[permission]); grid.append(checkbox); choices[permission] = checkbox
        box.append(grid)
        from .ai_agent_ui import AgentSelection
        selection = AgentSelection(self.window.ai_agents, box)
        def inputs():
            buffer = description.get_buffer()
            values = (entries['id'].get_text().strip(), entries['name'].get_text().strip(),
                    buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
                    {p for p, checkbox in choices.items() if checkbox.get_active()})
            from .plugin_manifest import valid_id
            if not valid_id(values[0]): raise ValueError('Use a nonreserved lowercase-with-hyphens plugin ID')
            if not values[1] or len(values[1]) > 128: raise ValueError('Name required; at most 128 characters')
            if len(values[2]) > 8192: raise ValueError('Description exceeds 8192 characters')
            if (self.catalog.store.directory/values[0]).exists(): raise ValueError('Plugin already exists; choose another ID')
            return values, selection.values()
        def create(values):
            scaffold_values, agent_values = values
            root = scaffold(self.catalog.store.directory, *scaffold_values)
            self.rediscover()
            spec, _, _ = inspect_manifest(root)
            self.last_prompt = agent_prompt(root, spec.description, spec.permissions)
            provider, model = agent_values
            prompt = self.last_prompt
            self.text_dialog('Plugin folder created',
                f'Plugin folder created: {root}\n\n1. Open a terminal and change into that folder.\n'
                '2. Start your coding agent and paste this prompt.\n3. Return to Naui and Enable the plugin, or use Reload plugin from its menu. Both review and check the code first.\n\n' + self.last_prompt,
                prompt=True, actions=[('Start AI Agent', lambda: self.agents.start(spec.id, provider, prompt, model))] if provider != 'manual' else [])
        dialog = self.dialog('Create Plugin with AI', box, create, accept='Create Scaffold', prepare=inputs)
        if dialog: self.dialog_controls[dialog] = selection.signals
        else: selection.signals.close()

    def copy_master(self):
        from .plugin_sdk import sdk_root
        self.copy((sdk_root()/'AGENT_INSTRUCTIONS.md').read_text())

    def reveal(self, directory):
        local = self.window.workspace.devices['local']
        local.add_root(str(directory)); self.window.activate_device('local', reconnect=False)
        self.window.persist_workspace(); self.window.update_workspace_ui()

    def open_sdk(self):
        from .plugin_sdk import sdk_root
        self.reveal(sdk_root())

    def open_user_plugins(self):
        self.catalog.store.directory.mkdir(parents=True, exist_ok=True)
        self.reveal(self.catalog.store.directory)

    def modify_dialog(self, identity):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(label('Requested change'))
        entry = Gtk.Entry(); box.append(entry)
        self.dialog('Modify ' + self.catalog.records[identity].spec.name, box,
                    lambda request: self.modification(identity, request), accept='Generate Prompt', prepare=entry.get_text)

    def modification(self, identity, request):
        record = self.catalog.records[identity]
        self.last_prompt = agent_prompt(record.directory, request, record.spec.permissions, mode='modify')
        self.text_dialog('AI Modification Prompt', self.last_prompt, prompt=True)

    def copy_error(self, identity):
        record = self.catalog.records[identity]
        self.last_prompt = agent_prompt(record.directory, 'Fix the validation/runtime error and preserve existing behavior.',
            record.spec.permissions, mode='modify', errors=record.validation.text() if record.validation and not record.validation.ok else record.error)
        self.copy(self.last_prompt)

    def choose(self, zipped):
        if self.window.dialog: return
        chooser = Gtk.FileChooserNative(title='Install plugin from ZIP' if zipped else 'Install plugin from Folder',
            transient_for=self.window, modal=True, action=Gtk.FileChooserAction.OPEN if zipped else Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label='Inspect', cancel_label='Cancel')
        self.dialogs.add(chooser); self.window.dialog = chooser
        def response(chooser, answer):
            path = chooser.get_file().get_path() if answer == Gtk.ResponseType.ACCEPT and chooser.get_file() else None
            self.signals.disconnect(chooser); self.dialogs.discard(chooser)
            if self.window.dialog is chooser: self.window.dialog = None
            chooser.destroy()
            if path: self.defer(lambda: self.prepare(path, zipped))
        self.signals.connect(chooser, 'response', response); chooser.show()

    def prepare(self, path, zipped=False):
        self.busy = True; self.refresh(); self.status.set_text('Inspecting plugin structure without executing code…')
        def done(candidate, error):
            if self._shutting_down:
                if candidate: candidate.close()
                return
            self.busy = False
            if error: self.status.set_text(str(error)); self.refresh(); return
            self.candidates.append(candidate)
            body = label(candidate.spec.name + '\n' + candidate.spec.version + '\n' + candidate.spec.description +
                         '\nSource: ' + candidate.source + '\n' + candidate.origin + '\nPermissions:\n' +
                         '\n'.join(PERMISSION_LABELS.get(p, p) for p in sorted(candidate.spec.permissions)) +
                         '\n\nNo plugin code has run. Install leaves it disabled. Validate and review trust before enabling.')
            def install():
                try:
                    root = candidate.install(self.catalog.store.directory)
                    self.catalog.store.remember(candidate.spec.id, False, [], '', candidate.source)
                    self.rediscover(); self.status.set_text('Plugin installed and disabled. Choose Enable to review permissions and check it before use.')
                finally:
                    candidate.close(); self.candidates.remove(candidate)
            dialog = self.dialog('Install Plugin', body, install, accept='Install Disabled',
                                 cancelled=lambda: self.cancel_candidate(candidate))
            if dialog is None: candidate.close(); self.candidates.remove(candidate)
            self.refresh()
        self.window.work(lambda: InstallCandidate(path, zipped=zipped), done)

    def cancel_candidate(self, candidate):
        if candidate in self.candidates: candidate.close(); self.candidates.remove(candidate)

    def open_folder(self, identity):
        root = str(self.catalog.records[identity].directory)
        local = self.window.workspace.devices['local']
        local.add_root(root)
        self.window.activate_device(local.id, reconnect=False)
        self.window.persist_workspace()
        self.window.update_workspace_ui()
        self.status.set_text('Plugin folder is available in the local workspace: ' + root)

    def terminal_here(self, identity):
        self.window.create_terminal(self.window.workspace.devices['local'], str(self.catalog.records[identity].directory))

    def uninstall(self, identity):
        record = self.catalog.records[identity]
        self.require_agent_stopped(identity)
        def remove():
            self.require_agent_stopped(identity)
            self.catalog.uninstall(identity); self.refresh()
        self.dialog('Uninstall ' + record.spec.name + '?', label('Remove only this plugin folder:\n' + str(record.directory) +
                    '\nPlugin settings are retained. Device files and other plugins are untouched.'),
                    remove, accept='Uninstall')

    def require_agent_stopped(self, identity):
        if any(task.plugin_id == identity and task.process and not task.cleaned
               for task in self.window.ai_agents.tasks.values()):
            raise ValueError('Stop the AI task and wait for its terminal to close before enabling/reloading/uninstalling this plugin')

    def logs(self, identity):
        self.text_dialog('Plugin Logs — ' + identity, '\n'.join(self.manager.logs[identity]) or 'No plugin log entries.')

    def invoke(self, identity, command):
        # Restore the editor/terminal that was active when management opened;
        # editor commands see a document rather than the manager tab itself.
        page = self.window.notebook.page_num(self.return_page) if self.return_page else -1
        if page >= 0: self.window.notebook.set_current_page(page)
        self.manager.invoke(identity, command)

    def mark_disconnected(self, reason=''): pass
    def update_label(self): pass

    def close(self):
        if self._shutting_down: return
        self._shutting_down = True
        self.row_signals.close(); self.signals.close()
        for controls in self.dialog_controls.values(): controls.close()
        self.dialog_controls.clear()
        for directory in self.validation_paths: self.catalog.validator.cancel(directory)
        self.validation_paths.clear()
        for candidate in self.candidates: candidate.close()
        self.candidates.clear()
        for dialog in self.dialogs:
            if self.window.dialog is dialog: self.window.dialog = None
            dialog.destroy()
        self.dialogs.clear()
        self.return_page = None

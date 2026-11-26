"""Provider settings and task controls hosted by the existing Plugin Manager."""
import shlex
from gi.repository import Gtk
from .ai_agents import plugin_snapshot, review_changes, FINAL_STATES
from .lifecycle import Signals
from .plugin_sdk import agent_prompt


class AgentSelection:
    def __init__(self, manager, box):
        self.manager = manager
        self.options = [('manual', 'Manual copy mode')] + [(p.id, p.name + ' — ' + p.status()) for p in manager.providers()]
        box.append(Gtk.Label(label='AI agent/provider', xalign=0))
        self.dropdown = Gtk.DropDown.new_from_strings([name for _, name in self.options]); box.append(self.dropdown)
        self.model = Gtk.Entry(placeholder_text='Provider Default (optional model ID)')
        self.model.set_text(manager.settings.data['default_model']); box.append(self.model)
        index = next((i for i, (identity, _) in enumerate(self.options) if identity == manager.settings.data['default_provider']), 0)
        self.dropdown.set_selected(index)
        self.signals = Signals()
        self.signals.connect(self.dropdown, 'notify::selected', lambda *_: self.update())
        self.update()
    def update(self):
        provider = self.manager.provider(self.options[self.dropdown.get_selected()][0])
        self.model.set_visible(bool(provider and provider.supports_model))
        # Providers need not enumerate models. A typed provider-specific ID is
        # supported only by adapters that accept it; no obsolete model catalog.
    def values(self):
        identity = self.options[self.dropdown.get_selected()][0]
        provider = self.manager.provider(identity)
        model = self.model.get_text().strip() if provider and provider.supports_model else ''
        if identity != 'manual' and (not provider or not provider.available()): raise ValueError(provider.status() if provider else 'Provider unavailable')
        return identity, model


class AiAgentUI:
    def __init__(self, view):
        self.view, self.window, self.manager = view, view.window, view.window.ai_agents
    def settings(self):
        view = self.view
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Label(label='CLI agents keep their own authentication. Arguments are stored as plain settings: never enter credentials.', wrap=True))
        if self.manager.settings.error:
            box.append(Gtk.Label(label='Existing AI settings invalid (preserved): ' + self.manager.settings.error, wrap=True))
        selection = AgentSelection(self.manager, box)
        box.append(Gtk.Label(label='Custom CLI (leave name/executable blank to remove)', xalign=0))
        custom = self.manager.settings.data['custom'] or {}
        fields = []
        for title, value in [('Name', custom.get('name', '')), ('Executable', custom.get('executable', '')),
                             ('Non-secret arguments (shell quoting accepted, no shell evaluation)', shlex.join(custom.get('arguments', [])))]:
            box.append(Gtk.Label(label=title, xalign=0)); entry = Gtk.Entry(text=value); box.append(entry); fields.append(entry)
        box.append(Gtk.Label(label='Prompt delivery: prompt-file uses {prompt_file} in argv; terminal waits for explicit Send Prompt.', wrap=True))
        modes = ['stdin', 'terminal', 'prompt-file']
        delivery = Gtk.DropDown.new_from_strings(modes); delivery.set_selected(modes.index(custom.get('delivery', 'stdin'))); box.append(delivery)
        def values():
            # Allow saving new custom configuration and then selecting it on
            # the next open; no execution occurs while configuring providers.
            identity = selection.options[selection.dropdown.get_selected()][0]
            if identity == 'api': raise ValueError('API provider framework only; no task adapter or credential storage implemented')
            model = selection.model.get_text().strip() if selection.model.get_visible() else ''
            name, executable, arguments = [entry.get_text().strip() for entry in fields]
            config = {'name': name, 'executable': executable, 'arguments': shlex.split(arguments), 'delivery': modes[delivery.get_selected()]} if name or executable else None
            data = {'schema': 1, 'default_provider': identity, 'default_model': model, 'custom': config}
            from .ai_agents import AiAgentSettings, executable_path
            AiAgentSettings.check(data)
            if identity == 'custom' and config is None: raise ValueError('Configure the custom agent before making it the default')
            if config: executable_path(executable)
            return data
        dialog = view.dialog('AI Agents', box, self.manager.settings.save, accept='Save Settings', prepare=values)
        if dialog: view.dialog_controls[dialog] = selection.signals
        else: selection.signals.close()
    def guide(self):
        self.view.text_dialog('AI Plugin Guide',
            'Manual mode: create scaffold, copy the ready-to-paste prompt, use any coding agent, then Validate/Enable.\n\n'
            'Codex / Claude: choose a detected CLI and optional model ID. Start AI Agent opens an independent real PTY in the plugin directory. '
            'Use the CLI’s normal approvals/login; exit the CLI when done. Naui never auto-enables generated code.\n\n'
            'Custom: configure an executable and argv in AI Agents. stdin gives a finite prompt file as stdin. '
            'prompt-file replaces {prompt_file} in argv. terminal launches the CLI first; click Send Prompt only once its input UI is ready.\n\n'
            'API mode is a future provider/credential-reference contract. No live API adapter or token storage is implemented.\n\n'
            'Prompts enforce a plugin-only instruction boundary, not a universal OS sandbox. Agents run as your user, can read local files '
            'and may maintain their own history. Naui does not forward SSH sockets/passwords, Naui tokens or unrelated API credentials.\n\n'
            'Review Changes → Validate → Enable/Reload. Send Fix creates a fresh task; it never guesses another agent session ID.')
    def choose(self, identity, *, repair=False):
        record = self.view.catalog.records[identity]
        errors = record.validation.text() if record.validation and not record.validation.ok else record.error
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Label(label='Target: ' + str(record.directory), xalign=0, wrap=True))
        box.append(Gtk.Label(label='Requested change (no credentials)', xalign=0))
        request = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        request.set_size_request(-1, 100)
        request.get_buffer().set_text('Fix the reported error while preserving behavior.' if repair else record.spec.description)
        box.append(request)
        selection = AgentSelection(self.manager, box)
        def values():
            provider, model = selection.values()
            buffer = request.get_buffer()
            description = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            if not description.strip() or len(description) > 8192: raise ValueError('Describe the task (at most 8192 characters)')
            prompt = agent_prompt(record.directory, description, record.spec.permissions, mode='modify', errors=errors if repair else '')
            return provider, model, prompt
        dialog = self.view.dialog('Send Fix to Agent' if repair else 'Start AI Agent', box,
            lambda values: self.start(identity, values[0], values[2], values[1]) if values[0] != 'manual' else
                self.view.text_dialog('Ready-to-Paste Prompt', values[2], prompt=True),
            accept='Start / Copy Prompt', prepare=values)
        if dialog: self.view.dialog_controls[dialog] = selection.signals
        else: selection.signals.close()
    def start(self, identity, provider, prompt, model=''):
        view, manager = self.view, self.manager
        directory = str(view.catalog.records[identity].directory)
        view.busy = True; view.refresh(); view.status.set_text('Capturing plugin changes before starting agent…')
        def done(before, error):
            if view._shutting_down: return
            view.busy = False
            if error: view.status.set_text(str(error)); view.refresh(); return
            view.attempt(lambda: manager.start_task(provider, directory, prompt, model=model or None, before=before))
            view.refresh()
        self.window.work(lambda: plugin_snapshot(directory), done)
    def open_terminal(self, task):
        terminal = self.window.terminals.get(task.terminal_id)
        if terminal:
            self.window.notebook.set_current_page(self.window.notebook.page_num(terminal)); terminal.view.grab_focus()
        else: self.view.status.set_text('Agent terminal has exited. Task status and file changes remain available.')
    def changes(self, task):
        directory, before = task.cwd, task.before
        def done(text, error):
            if self.view._shutting_down: return
            if error: self.view.status.set_text('Review unavailable: ' + str(error))
            else: self.window.open_text_view('local', 'AI Changes — ' + task.plugin_id, text, 'diff')
        self.window.work(lambda: review_changes(directory, before), done)
    def render(self, box):
        view = self.view
        for task in self.manager.tasks.values():
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            row.append(Gtk.Label(label=task.plugin_id + ' · ' + task.provider_id + ' · ' + task.state, xalign=0))
            if task.error: row.append(Gtk.Label(label=task.error, xalign=0, wrap=True))
            actions = Gtk.Box(spacing=6)
            if task.terminal_id in self.window.terminals:
                view.button(actions, 'Open terminal', lambda task=task: self.open_terminal(task))
            if task.state not in FINAL_STATES:
                view.button(actions, 'Stop', lambda task=task: self.manager.stop_task(task.id))
            view.button(actions, 'View Changes', lambda task=task: self.changes(task))
            if task.delivery == 'terminal' and task.state == 'running' and not task.prompt_sent:
                view.button(actions, 'Send Prompt (agent ready)', lambda task=task: self.manager.send_prompt(task.id), task.state == 'running' and not task.prompt_sent)
            row.append(actions); box.append(row)

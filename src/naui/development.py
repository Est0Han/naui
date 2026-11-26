"""Everyday development controls over the existing command and document owners."""
import json
import os
from pathlib import Path
import posixpath
import shlex
import tempfile

from gi.repository import Gdk, Gtk
from .lifecycle import Signals


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.naui-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path, limit, default):
    try:
        if path.stat().st_size > limit: raise ValueError('Saved development data exceeds its limit')
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def choose(window, title, items, callback, hint='Type to filter…', initial_query=''):
    """Searchable, keyboard-operable chooser. Items are (label, value)."""
    if window.dialog:
        window.dialog.present(); return
    dialog = Gtk.Dialog(title=title, transient_for=window, modal=True)
    dialog.set_default_size(660, 440)
    dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
    dialog.add_button('Select', Gtk.ResponseType.ACCEPT)
    box = dialog.get_content_area(); box.set_spacing(8)
    entry = Gtk.SearchEntry(placeholder_text=hint)
    entry.set_text(initial_query)
    box.append(entry)
    listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
    scroll = Gtk.ScrolledWindow(vexpand=True); scroll.set_child(listing); box.append(scroll)
    signals, shown = Signals(), []
    def populate(*_):
        while listing.get_first_child(): listing.remove(listing.get_first_child())
        words = entry.get_text().casefold().split()
        matches = [(label, value) for label, value in items if all(word in label.casefold() for word in words)]
        needle = entry.get_text().casefold().strip()
        if needle:
            matches.sort(key=lambda item: (item[0].casefold() != needle, not item[0].casefold().startswith(needle)))
        shown[:] = matches[:100]
        for text, value in shown:
            label = Gtk.Label(label=text, xalign=0, wrap=True)
            label.set_margin_top(6); label.set_margin_bottom(6)
            listing.append(label)
        listing.select_row(listing.get_row_at_index(0))
    def key(_, key, code, modifiers):
        if key in (Gdk.KEY_Down, Gdk.KEY_Up):
            row = listing.get_selected_row()
            index = row.get_index() if row else 0
            listing.select_row(listing.get_row_at_index(max(0, min(len(shown)-1, index + (1 if key == Gdk.KEY_Down else -1)))))
            return True
        if key == Gdk.KEY_Escape:
            dialog.response(Gtk.ResponseType.CANCEL); return True
        return False
    def response(_, answer):
        row = listing.get_selected_row()
        value = shown[row.get_index()][1] if answer == Gtk.ResponseType.ACCEPT and row else None
        signals.close(); window.dialog = None; dialog.destroy()
        if value is not None:
            window.sources.idle(lambda: callback(value) if not window._shutting_down else None)
    signals.connect(entry, 'search-changed', populate)
    signals.connect(entry, 'activate', lambda *_: dialog.response(Gtk.ResponseType.ACCEPT))
    signals.connect(listing, 'row-activated', lambda *_: dialog.response(Gtk.ResponseType.ACCEPT))
    signals.connect(dialog, 'response', response)
    keys = Gtk.EventControllerKey(); signals.connect(keys, 'key-pressed', key); entry.add_controller(keys)
    window.dialog = dialog
    populate(); dialog.present(); entry.grab_focus()


class Development:
    def __init__(self, window):
        self.window = window
        self.recent = []
        self.tasks_path = window.workspace_store.path.with_name('tasks.json')
        self.tasks = []
        self.tasks_valid = True
        try:
            values = read_json(self.tasks_path, 256*1024, [])
            if not isinstance(values, list) or len(values) > 128: raise ValueError('Invalid task list')
            for item in values: self.validate_task(item)
            self.tasks = values
        except (ValueError, OSError, TypeError, KeyError) as error:
            self.tasks_valid = False
            window.set_status('Tasks could not be loaded: ' + str(error))

    @staticmethod
    def validate_task(task):
        from .commands import validate_command
        if not isinstance(task, dict) or set(task) != {'name','device','cwd','argv'}:
            raise ValueError('Invalid project task')
        if not isinstance(task['name'], str) or not 0 < len(task['name']) <= 120: raise ValueError('Task name required (120 characters maximum)')
        if not isinstance(task['device'], str) or not 0 < len(task['device']) <= 64: raise ValueError('Invalid task device')
        validate_command(task['argv'], task['cwd'])

    def palette(self):
        from .keybinding_host import focused_context
        w = self.window
        if w.notebook.current: w.notebook._focus(w.notebook.current)
        context = focused_context(w)
        origin = w.notebook.current
        available = []
        for command in w.command_registry.commands.values():
            if command.context not in ('global', context): continue
            try:
                if not command.enabled(): continue
            except Exception: continue
            shortcut = w.command_registry.binding(command.id)
            available.append((command.category + ' · ' + command.title + ('   ' + shortcut if shortcut else ''), command.id))
        def run(identity):
            command = w.command_registry.commands.get(identity)
            if command is None: return
            if origin in w.notebook.pages: w.notebook.set_current_page(w.notebook.page_num(origin))
            try:
                if command.enabled(): command.callback()
            except Exception as error: w.set_status(str(error))
        choose(w, 'Command Palette', sorted(available), run, 'Search actions…')

    def closed_editor(self, editor):
        self.recent = [item for item in self.recent if (item['device'],item['path']) != (editor.device_id,editor.path)]
        self.recent.append(dict(device=editor.device_id, path=editor.path,
            offset=editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset()))
        self.recent = self.recent[-20:]

    def reopen(self):
        if not self.recent: self.window.set_status('No recently closed file tabs.'); return
        item = self.recent.pop()
        w = self.window
        device = w.workspace.devices.get(item['device'])
        if device is None: w.set_status('This file’s device is no longer in the workspace.'); return
        def open_file():
            editor = w.open_file(device.filesystem_backend, item['path'], device.session_id)
            if editor:
                editor.restore_offset = item['offset']
                if not editor.loading:
                    from .session_restore import SessionRestore
                    SessionRestore.place(editor)
        if not device.connected:
            self.recent.append(item)
            w.set_status('Reconnect ' + device.display_name + ' from Devices, then reopen the tab.'); return
        open_file()

    def task_context(self):
        w = self.window
        editor = w.current_editor()
        device = w.workspace.devices[editor.device_id if editor else w.workspace.active_device_id]
        path = editor.path if editor else device.navigation_state.current_path
        roots = [root.path for root in device.roots if path and (path == root.path or path.startswith(root.path.rstrip('/')+'/'))]
        cwd = max(roots, key=len) if roots else posixpath.dirname(path) if editor else path
        if not cwd: raise ValueError('Choose a project directory first')
        return device, cwd

    def add_task(self):
        w = self.window
        if w.dialog: w.dialog.present(); return
        try: device, cwd = self.task_context()
        except ValueError as error: w.set_status(str(error)); return
        dialog = Gtk.Dialog(title='Save Project Task', transient_for=w, modal=True)
        dialog.set_default_size(600, 300)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL); dialog.add_button('Save Task', Gtk.ResponseType.ACCEPT)
        area = dialog.get_content_area(); area.set_spacing(8)
        area.append(Gtk.Label(label='Device: ' + device.display_name + '\nCommands run only when you choose Run Task.', xalign=0))
        entries = []
        for title, value in [('Name (Build, Test, Run, Format…)', ''), ('Working directory', cwd), ('Command and arguments', '')]:
            area.append(Gtk.Label(label=title, xalign=0)); entry=Gtk.Entry(text=value); area.append(entry); entries.append(entry)
        area.append(Gtk.Label(label='Use quoted arguments. For shell pipelines, explicitly use bash -lc "…".', xalign=0, wrap=True))
        error_label = Gtk.Label(xalign=0, wrap=True); area.append(error_label)
        signals = Signals()
        def response(_, answer):
            if answer == Gtk.ResponseType.ACCEPT:
                try:
                    if not self.tasks_valid: raise ValueError('Repair the invalid tasks.json file before saving tasks')
                    task=dict(name=entries[0].get_text().strip(),device=device.id,cwd=entries[1].get_text(),argv=shlex.split(entries[2].get_text()))
                    self.validate_task(task)
                    updated=[item for item in self.tasks if (item['name'],item['device'],item['cwd']) != (task['name'],task['device'],task['cwd'])]+[task]
                    if len(updated)>128: raise ValueError('At most 128 tasks can be saved')
                    atomic_json(self.tasks_path, updated); self.tasks=updated
                except (ValueError,OSError) as error: error_label.set_text(str(error)); return
            signals.close(); w.dialog=None; dialog.destroy()
        signals.connect(dialog,'response',response); w.dialog=dialog; dialog.present()

    def run_task(self):
        w=self.window
        if not self.tasks: self.add_task(); return
        def label(task):
            device=w.workspace.devices.get(task['device'])
            return task['name']+' · '+(device.display_name if device else 'Unavailable device')+' · '+task['cwd']
        def review(task):
            device=w.workspace.devices.get(task['device'])
            if device is None: w.set_status('Task device is no longer available.'); return
            def run():
                try: w.queue_run(device.id, task['cwd'], task['argv'], new_terminal=True)
                except (OSError, ValueError, RuntimeError) as error: w.set_status(str(error))
            w.confirm_action('Run '+task['name']+'?', 'Device: '+device.display_name+'\nDirectory: '+task['cwd']+'\nCommand: '+shlex.join(task['argv']), 'Run in New Terminal', run)
        choose(w, 'Run Project Task', [(label(task), task) for task in self.tasks], review)

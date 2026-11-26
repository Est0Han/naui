"""Named presentation layouts. No files, connections, tasks or editor contents."""
from copy import deepcopy
from .development import atomic_json, read_json
from .ui_settings import UISettings

PRESENTATION = frozenset(('right_panel_width', 'right_panel_collapsed', 'right_panel_selected', 'sidebar_width', 'sidebar_collapsed', 'sidebar_panel', 'sidebar_scale',
    'sidebar_density', 'window_width', 'window_height', 'editor_font', 'terminal_font',
    'editor_wrap', 'editor_line_numbers', 'editor_current_line', 'editor_whitespace',
    'devices_expanded', 'files_expanded', 'source_control_expanded',
    'explorer_tree_height', 'explorer_hide_disconnected', 'explorer_device_expanded',
    'explorer_device_heights', 'split_ratio'))


class PresetStore:
    def __init__(self, path):
        self.path, self.valid, self.presets, self.error = path, True, {}, ''
        try:
            data = read_json(path, 512*1024, {'version': 1, 'presets': {}})
            if not isinstance(data, dict) or data.get('version') != 1: raise ValueError('Invalid layout presets')
            self.validate(data.get('presets'))
            self.presets = data['presets']
        except (OSError, ValueError, TypeError) as error:
            self.valid, self.error = False, str(error)

    @staticmethod
    def name(value):
        if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 60 or not value.isprintable():
            raise ValueError('Use a name of 1–60 visible characters.')
        return value

    @classmethod
    def validate(cls, presets):
        if not isinstance(presets, dict) or len(presets) > 32: raise ValueError('At most 32 layout presets are supported.')
        for name, preset in presets.items():
            cls.name(name)
            if not isinstance(preset, dict) or set(preset) != {'layout', 'settings'}: raise ValueError('Invalid layout preset')
            if preset['layout'] not in ('single', 'horizontal', 'vertical'): raise ValueError('Invalid split arrangement')
            settings = preset['settings']
            if not isinstance(settings, dict) or not set(settings) <= PRESENTATION: raise ValueError('Invalid presentation settings')
            UISettings.validate(settings)

    def commit(self, presets):
        if not self.valid: raise ValueError('Existing layout presets could not be read; the original file is preserved. ' + self.error)
        self.validate(presets)
        atomic_json(self.path, {'version': 1, 'presets': presets})
        self.presets = deepcopy(presets)

    def save(self, name, preset, replace=False):
        self.name(name)
        if name in self.presets and not replace: raise ValueError('That name already exists. Choose another name or Update Saved Preset.')
        self.commit(dict(self.presets, **{name: deepcopy(preset)}))

    def rename(self, old, new):
        self.name(new)
        if old not in self.presets: raise ValueError('Preset no longer exists')
        if new != old and new in self.presets: raise ValueError('That name already exists')
        updated = dict(self.presets); value = updated.pop(old); updated[new] = value
        self.commit(updated)

    def delete(self, name):
        updated = dict(self.presets); updated.pop(name)
        self.commit(updated)


class LayoutPresets:
    def __init__(self, window):
        from gi.repository import Gtk
        self.window = window
        self.store = PresetStore(window.workspace_store.path.with_name('layouts.json'))
        self.button = Gtk.MenuButton(label='Layout Presets')
        self.popover = Gtk.Popover()
        self.button.set_popover(self.popover)
        window.signals.connect(self.popover, 'notify::visible', lambda *_: self.populate() if self.popover.get_visible() else None)
        self.populate()

    def capture(self):
        from gi.repository import Gtk
        w = self.window
        w.save_ui()
        pane = w.notebook.paned
        return {'layout': 'single' if not pane else 'horizontal' if pane.get_orientation() == Gtk.Orientation.HORIZONTAL else 'vertical',
                'settings': deepcopy({key: value for key, value in w.ui_settings.values.items() if key in PRESENTATION})}

    def populate(self):
        from gi.repository import Gtk
        # Release the previous controls and their handlers on every refresh.
        old = self.popover.get_child()
        if old:
            box = old.get_child()
            child = box.get_first_child()
            while child:
                self.window.signals.disconnect(child); child = child.get_next_sibling()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        choices = [(name, lambda name=name: self.apply(name)) for name in sorted(self.store.presets, key=str.casefold)]
        choices += [('Save Current Layout…', lambda: self.name_dialog()),
                    ('Update Saved Preset…', lambda: self.select('update')),
                    ('Rename Preset…', lambda: self.select('rename')),
                    ('Delete Preset…', lambda: self.select('delete'))]
        for label, action in choices:
            button = Gtk.Button(label=label)
            self.window.signals.connect(button, 'clicked', lambda *_, action=action: (self.popover.popdown(), action()))
            box.append(button)
        scroll = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=450, min_content_width=260)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(box); self.popover.set_child(scroll)

    def name_dialog(self, old=None):
        from gi.repository import Gtk
        from .lifecycle import Signals
        w = self.window
        if w.dialog: w.dialog.present(); return
        preset = self.capture() if old is None else None
        dialog = Gtk.Dialog(title='Rename Layout Preset' if old else 'Save Layout Preset', transient_for=w, modal=True)
        dialog.set_default_size(420, 160)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL); dialog.add_button('Rename' if old else 'Save', Gtk.ResponseType.ACCEPT)
        area = dialog.get_content_area(); area.set_spacing(8)
        area.append(Gtk.Label(label='Preset name', xalign=0))
        entry = Gtk.Entry(text=old or '', max_length=60, activates_default=True)
        area.append(entry); error_label = Gtk.Label(wrap=True, xalign=0); area.append(error_label)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        signals = Signals()
        def response(_, answer):
            if answer == Gtk.ResponseType.ACCEPT:
                try:
                    name = entry.get_text().strip()
                    if old is None: self.store.save(name, preset)
                    else: self.store.rename(old, name)
                except (ValueError, OSError) as error: error_label.set_text(str(error)); return
                w.set_status('Layout preset saved: ' + name)
            signals.close(); w.dialog = None; dialog.destroy()
        signals.connect(dialog, 'response', response)
        w.dialog = dialog; dialog.present(); entry.grab_focus()

    def select(self, action):
        from .development import choose
        w = self.window
        if not self.store.presets:
            w.set_status('Save a layout preset first.' if self.store.valid else self.store.error); return
        def selected(name):
            if action == 'rename': self.name_dialog(name)
            elif action == 'update':
                try: self.store.save(name, self.capture(), replace=True)
                except (OSError, ValueError) as error: w.set_status(str(error)); return
                w.set_status('Updated layout preset: ' + name)
            else:
                def delete():
                    try: self.store.delete(name)
                    except (OSError, ValueError, KeyError) as error: w.set_status(str(error)); return
                    w.set_status('Deleted layout preset: ' + name)
                w.confirm_action('Delete layout preset?', name + '\nThe current layout stays as it is.', 'Delete Preset', delete)
        choose(w, action.title() + ' Layout Preset', [(name, name) for name in sorted(self.store.presets)], selected)

    def apply(self, name):
        from gi.repository import Gtk, Pango
        w = self.window
        preset = deepcopy(self.store.presets[name])
        if not w.ui_settings.valid:
            w.set_status('Reset Layout first to repair the invalid UI settings file.'); return
        try: w.ui_settings.save(**preset['settings'])
        except OSError as error: w.set_status(str(error)); return
        settings = deepcopy(w.ui_settings.values)
        w.sidebar_width, w.sidebar_collapsed = settings['sidebar_width'], settings['sidebar_collapsed']
        panel = settings['sidebar_panel'] if settings['sidebar_panel'] in w.left_sidebar.state.panels else 'explorer'
        w.left_sidebar.state.select(panel); w.left_sidebar._display()
        w.left_sidebar.scale_control.set_value(settings['sidebar_scale'])
        w.left_sidebar.density_control.set_active(0 if settings['sidebar_density'] == 'compact' else 1)
        w.left_sidebar.apply_style()
        sidebar = w.workspace_sidebar
        sidebar.tree_height.set_value(settings['explorer_tree_height'])
        # The global height control intentionally resets manual heights; restore
        # the chosen preset after that signal has run.
        w.ui_settings.values['explorer_device_heights'] = settings['explorer_device_heights']
        sidebar.hide_disconnected.set_active(settings['explorer_hide_disconnected'])
        sidebar.devices_section.set_expanded(settings['devices_expanded'])
        sidebar.files_section.set_expanded(settings['files_expanded'])
        for explorer in sidebar.explorers.values(): explorer.apply_tree_size()
        for identity, card in sidebar.cards.items():
            card[0].set_expanded(settings['explorer_device_expanded'].get(identity, w.workspace.devices[identity].connected))
        sidebar.update_devices()
        for section in w.sidebar_sections.values(): section.set_expanded(settings['source_control_expanded'])
        w.left_sidebar.set_visible(not w.sidebar_collapsed)
        w.split.set_position(w.sidebar_width)
        if preset['layout'] == 'single': w.notebook.single_view()
        else: w.notebook.split(Gtk.Orientation.HORIZONTAL if preset['layout'] == 'horizontal' else Gtk.Orientation.VERTICAL)
        w.right_panels.apply_layout()
        w.apply_editor_font()
        for terminal in w.terminals.values(): terminal.view.set_font(Pango.FontDescription.from_string('Monospace ' + str(settings['terminal_font'])))
        for editor in w.editors: editor.editing.configure()
        w.set_default_size(settings['window_width'], settings['window_height'])
        w.save_ui()
        suffix = ' — open a second tab to use its split arrangement' if preset['layout'] != 'single' and not w.notebook.paned else ''
        w.set_status('Applied layout preset: ' + name + suffix)

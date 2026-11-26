"""GtkSourceView adapter and view-scoped editor commands.

No file/SSH/process operations live here. Native buffer edits are grouped for
undo, search callbacks are cancellable, and deactivation precedes page removal.
"""
from gi.repository import Gdk, Gio, GLib, Gtk, GtkSource, Pango

from .editor_commands import LINE_COMMANDS, line_edit
from .editor_model import LARGE_CHARACTERS, LARGE_LINES, LARGE_LINE_LENGTH, shebang_language


class GtkBufferAdapter:
    def __init__(self, buffer): self.buffer = buffer
    def text(self): return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)
    @property
    def dirty(self): return self.buffer.get_modified()


class EditorController:
    def __init__(self, pane):
        self.pane, self.closed = pane, False
        self.dialogs = set()
        self.pending_search = None
        self.search_generation = 0
        self.comment = None
        self.actions = Gio.SimpleActionGroup()
        pane.view.insert_action_group('editor', self.actions)
        self.menu_entries = []
        menu = Gio.Menu()
        for label, command in [('Toggle Comment', 'toggle_comment'), ('Indent Lines', 'indent'),
                               ('Outdent Lines', 'outdent'), ('Move Lines Up', 'move_up'),
                               ('Move Lines Down', 'move_down'), ('Duplicate Lines', 'duplicate'),
                               ('Delete Lines', 'delete_line'), ('Find…', 'find'), ('Replace…', 'replace'),
                               ('Go to Line…', 'goto_line'), ('Go to Definition', 'definition'),
                               ('Find References', 'references'), ('Copy Path', 'copy_path'),
                               ('Reveal in File Tree', 'reveal')]:
            action = Gio.SimpleAction.new(command.replace('_','-'), None)
            pane.signals.connect(action, 'activate', lambda *_, command=command: self.execute(command))
            self.actions.add_action(action)
            self.menu_entries.append((label,command))
            menu.append(label, 'editor.' + command.replace('_','-'))
        pane.view.set_extra_menu(menu)
        self.update_menu()
        self._search_ui()
        pane.signals.connect(pane.buffer, 'changed', self.changed)
        self.configure()

    def update_menu(self):
        if self.closed: return
        from .keybinding_host import EDITOR_COMMANDS
        mapping = {action: 'editor.' + identity for identity, title, default, action in EDITOR_COMMANDS}
        mapping.update(definition='navigation.definition', references='navigation.references')
        registry = self.pane.window.command_registry
        menu = Gio.Menu()
        for title, action in self.menu_entries:
            identity = mapping.get(action)
            binding = registry.binding(identity) if identity in registry.commands else None
            menu.append(title + ('    '+binding if binding else ''), 'editor.' + action.replace('_','-'))
        self.pane.view.set_extra_menu(menu)

    def alive(self):
        return not self.closed and not self.pane._shutting_down and not self.pane.window._shutting_down

    def configure(self):
        if not self.alive(): return
        p, settings = self.pane, self.pane.window.ui_settings.values
        p.view.set_tab_width(settings['editor_tab_width'])
        p.view.set_indent_width(settings['editor_indent_width'])
        p.view.set_insert_spaces_instead_of_tabs(settings['editor_spaces'])
        p.view.set_indent_on_tab(False)  # Selection indentation belongs to the configurable command.
        p.view.set_auto_indent(settings['editor_auto_indent'])
        p.view.set_show_line_numbers(settings['editor_line_numbers'])
        p.view.set_highlight_current_line(settings['editor_current_line'])
        p.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if settings['editor_wrap'] else Gtk.WrapMode.NONE)
        drawer = p.view.get_space_drawer()
        drawer.set_types_for_locations(GtkSource.SpaceLocationFlags.ALL, GtkSource.SpaceTypeFlags.ALL)
        drawer.set_enable_matrix(settings['editor_whitespace'])
        p.buffer.set_highlight_matching_brackets(not p.document.large)
        p.buffer.set_highlight_syntax(not p.document.large)
        self.regex.set_sensitive(not p.document.large)
        if p.document.large: self.regex.set_active(False)
        self.replace_all_button.set_sensitive(not p.document.large)
        self.actions.lookup_action('toggle-comment').set_enabled(bool(self.comment) and not p.loading)
        for name in LINE_COMMANDS - {'toggle_comment'}:
            self.actions.lookup_action(name.replace('_','-')).set_enabled(not p.loading and p.revision is not None)
        for name in ('definition', 'references'):
            self.actions.lookup_action(name).set_enabled(not p.document.large and p.document.language in {'python','python3'}
                                                       and hasattr(p.window, 'code_navigation'))
        self.mode_label.set_text('Large file mode — syntax, LSP, regex and Replace All disabled'
                                 if p.document.large else '')
        self.mode_label.set_visible(p.document.large)

    def loaded(self, content):
        manager = GtkSource.LanguageManager.get_default()
        language = manager.guess_language(self.pane.path, None)
        if language is None:
            hint = shebang_language(content)
            language = manager.get_language(hint) if hint else None
        self.pane.document.language = language.get_id() if language else None
        self.pane.buffer.set_language(language)
        prefix = language.get_metadata('line-comment-start') if language else None
        suffix = ''
        if language and not prefix:
            prefix, suffix = language.get_metadata('block-comment-start'), language.get_metadata('block-comment-end')
        self.comment = (prefix, suffix or '') if prefix else None
        self.configure()

    def changed(self, *_):
        if not self.alive(): return
        self.cancel_search()
        p = self.pane
        p.document.lsp_version += 1
        # Cheap native counts; once in large mode, retain it until reload.
        if not p.document.large and (p.buffer.get_char_count() >= LARGE_CHARACTERS
                                    or p.buffer.get_line_count() > LARGE_LINES
                                    or p.buffer.get_iter_at_mark(p.buffer.get_insert()).get_chars_in_line() >= LARGE_LINE_LENGTH):
            p.document.large = True
            self.configure()

    def execute(self, name):
        if not self.alive(): return False
        p = self.pane
        if name in LINE_COMMANDS:
            if p.loading or p.revision is None: return False
            buffer = p.buffer
            anchor = buffer.get_iter_at_mark(buffer.get_selection_bound()).get_offset()
            cursor = buffer.get_iter_at_mark(buffer.get_insert()).get_offset()
            settings = p.window.ui_settings.values
            edit = line_edit(name, p.text(), anchor, cursor, comment=self.comment,
                             width=settings['editor_indent_width'], spaces=settings['editor_spaces'],
                             tab_width=settings['editor_tab_width'])
            if edit is None: return False
            self.apply(edit)
            return True
        actions = {'find': self.show_search, 'replace': lambda: self.show_search(True),
                   'goto_line': self.goto_line, 'next_occurrence': self.next_occurrence,
                   'copy_path': lambda: p.get_clipboard().set(p.path), 'reveal': self.reveal}
        if name in actions:
            actions[name]()
            return True
        if name in {'definition','references'} and not p.document.large and hasattr(p.window,'code_navigation'):
            p.window.code_navigation.query(p, name)
            return True
        raise ValueError('Unknown editor command: ' + str(name))

    def apply(self, edit):
        buffer = self.pane.buffer
        buffer.begin_user_action()
        try:
            buffer.delete(buffer.get_iter_at_offset(edit.start), buffer.get_iter_at_offset(edit.end))
            buffer.insert(buffer.get_iter_at_offset(edit.start), edit.replacement)
            buffer.select_range(buffer.get_iter_at_offset(edit.cursor), buffer.get_iter_at_offset(edit.anchor))
        finally:
            buffer.end_user_action()
        self.pane.view.scroll_mark_onscreen(buffer.get_insert())

    def reveal(self):
        p = self.pane
        device = p.window.workspace.devices.get(p.device_id)
        if device is None: return
        explorer = p.window.workspace_sidebar.explorers.get(p.device_id)
        if explorer:
            p.window.show_sidebar_panel('explorer')
            p.window.workspace_sidebar.cards[p.device_id][0].set_expanded(True)
            # Refresh/show that device's directory without injecting cd into a PTY.
            import posixpath
            explorer.pending_select = p.path
            parent = posixpath.dirname(p.path)
            explorer.set_context(p.backend, explorer.session, parent, explorer.home, explorer.connection_state)
            explorer.device.navigation_state.expanded.add(parent)
            explorer._restore_state()

    def _search_ui(self):
        p = self.pane
        self.search_settings = GtkSource.SearchSettings()
        self.search_settings.set_wrap_around(True)
        self.search_context = GtkSource.SearchContext.new(p.buffer, self.search_settings)
        box = p.search_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        row = Gtk.Box(spacing=3)
        self.entry = p.search_entry = Gtk.SearchEntry()
        self.entry.set_hexpand(True)
        row.append(self.entry)
        self.options = Gtk.Box(spacing=3)
        self.case = Gtk.CheckButton(label='Case')
        self.word = Gtk.CheckButton(label='Word')
        self.regex = Gtk.CheckButton(label='Regex')
        for control in (self.case,self.word,self.regex):
            control.set_tooltip_text({'Case':'Case sensitive','Word':'Whole words','Regex':'Regular expression search'}[control.get_label()])
            self.options.append(control)
            p.signals.connect(control,'toggled',self.search_changed)
        self.count = Gtk.Label(label='', xalign=0)
        self.count.set_ellipsize(Pango.EllipsizeMode.END)
        self.count.set_max_width_chars(30)
        self.options.append(self.count)
        for icon, hint, callback in [('go-up-symbolic','Previous match',lambda *_: self.find_next(True)),
                                     ('go-down-symbolic','Next match',lambda *_: self.find_next()),
                                     ('window-close-symbolic','Close search',lambda *_: self.hide_search())]:
            button = Gtk.Button.new_from_icon_name(icon)
            button.set_tooltip_text(hint)
            p.signals.connect(button,'clicked',callback)
            row.append(button)
        self.replace_row = Gtk.Box(spacing=3)
        self.replacement = Gtk.Entry(placeholder_text='Replace with…',hexpand=True)
        self.replace_row.append(self.replacement)
        for label, callback in [('Replace',self.replace_one),('Replace All',self.replace_all)]:
            button = Gtk.Button(label=label)
            p.signals.connect(button,'clicked',lambda *_,callback=callback: callback())
            self.replace_row.append(button)
            if label == 'Replace All': self.replace_all_button = button
        self.mode_label = Gtk.Label(xalign=0, wrap=True)
        p.append(self.mode_label)
        for child in (row,self.options,self.replace_row): box.append(child)
        self.replace_row.set_visible(False)
        box.set_visible(False)
        p.append(box)
        p.signals.connect(self.entry,'search-changed',self.search_changed)
        p.signals.connect(self.entry,'activate',lambda *_: self.find_next())
        p.signals.connect(self.replacement,'activate',lambda *_: self.replace_one())
        p.signals.connect(self.search_context,'notify::occurrences-count',self.update_count)
        p.signals.connect(self.search_context,'notify::regex-error',self.update_count)
        keys = Gtk.EventControllerKey()
        p.signals.connect(keys,'key-pressed',lambda _,key,*rest: (self.hide_search() or True) if key==Gdk.KEY_Escape else False)
        box.add_controller(keys)

    def show_search(self, replace=False):
        if not self.alive(): return
        p = self.pane
        selection = p.buffer.get_selection_bounds()
        if selection:
            text = p.buffer.get_text(*selection, True)
            if '\n' not in text and len(text) <= 4096:
                self.entry.set_text(text)
        p.search_row.set_visible(True)
        self.replace_row.set_visible(replace)
        self.search_context.set_highlight(True)
        self.entry.grab_focus()

    def hide_search(self):
        if not self.alive(): return
        self.cancel_search()
        self.pane.search_row.set_visible(False)
        self.search_context.set_highlight(False)
        self.pane.view.grab_focus()

    def search_changed(self, *_):
        if not self.alive(): return
        self.cancel_search()
        self.search_settings.set_case_sensitive(self.case.get_active())
        self.search_settings.set_at_word_boundaries(self.word.get_active())
        self.search_settings.set_regex_enabled(self.regex.get_active() and not self.pane.document.large)
        self.search_settings.set_search_text(self.entry.get_text() or None)
        self.update_count()

    def update_count(self, *_):
        if not self.alive(): return
        error = self.search_context.get_regex_error()
        count = self.search_context.get_occurrences_count()
        self.count.set_text('Invalid regex' if error else 'Searching…' if count < 0 else f'{count} matches')
        self.count.set_tooltip_text(error.message if error else None)

    def cancel_search(self):
        self.search_generation += 1
        if self.pending_search:
            self.pending_search.cancel()
            self.pending_search = None

    def find_next(self, backward=False):
        if not self.alive() or not self.entry.get_text(): return
        self.search_changed()  # Apply text immediately, even before SearchEntry debounce.
        if self.search_context.get_regex_error(): return
        self.cancel_search()
        generation = self.search_generation
        cancellable = self.pending_search = Gio.Cancellable()
        buffer = self.pane.buffer
        bounds = buffer.get_selection_bounds()
        start = (bounds[0] if backward else bounds[1]) if bounds else buffer.get_iter_at_mark(buffer.get_insert())
        def complete(context, result, *_):
            # Always finish the native task, including cancellation/stale work.
            # Only the guarded presentation step may touch the editor view.
            try:
                found, first, last, wrapped = (context.backward_finish if backward else context.forward_finish)(result)
            except GLib.Error as error:
                if self.alive() and generation == self.search_generation:
                    self.pending_search = None
                    self.count.set_text(error.message)
                return
            if not self.alive() or generation != self.search_generation: return
            self.pending_search = None
            if found:
                buffer.select_range(last, first)
                self.pane.view.scroll_to_iter(first,0.15,False,0,0)
            else: self.count.set_text('No matches')
        (self.search_context.backward_async if backward else self.search_context.forward_async)(start,cancellable,complete)

    def replace_one(self):
        if not self.alive() or self.pane.loading or self.pane.revision is None: return
        self.search_changed()
        bounds = self.pane.buffer.get_selection_bounds()
        try:
            if bounds:
                self.search_context.replace(*bounds,self.replacement.get_text(),-1)
            self.find_next()
        except GLib.Error as error: self.count.set_text(error.message)

    def replace_all(self):
        if not self.alive() or self.pane.document.large or self.pane.loading or self.pane.revision is None: return
        self.search_changed()
        try:
            count = self.search_context.replace_all(self.replacement.get_text(),-1)
            self.count.set_text(f'Replaced {count} matches')
        except GLib.Error as error: self.count.set_text(error.message)

    def next_occurrence(self):
        # Single-selection engine: move to the next occurrence, never simulate
        # extra selections/cursors that typing or native undo cannot honor.
        buffer = self.pane.buffer
        bounds = buffer.get_selection_bounds()
        if not bounds:
            start = buffer.get_iter_at_mark(buffer.get_insert())
            if not (start.inside_word() or start.starts_word() or start.ends_word()): return
            end = start.copy()
            if not start.starts_word(): start.backward_word_start()
            if not end.ends_word(): end.forward_word_end()
            if start.get_offset() == end.get_offset(): return
            buffer.select_range(end,start)
            bounds = (start,end)
        text = buffer.get_text(*bounds,True)
        if not text or len(text) > 4096: return
        self.regex.set_active(False)
        self.case.set_active(True)
        self.word.set_active(False)
        self.entry.set_text(text)
        self.find_next()

    def goto_line(self):
        if not self.alive() or self.pane.loading: return
        p = self.pane
        dialog = Gtk.Dialog(title='Go to Line',transient_for=p.window,modal=True)
        dialog.add_button('Cancel',Gtk.ResponseType.CANCEL)
        dialog.add_button('Go',Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        entry = Gtk.SpinButton.new_with_range(1,p.buffer.get_line_count(),1)
        entry.set_value(p.buffer.get_iter_at_mark(p.buffer.get_insert()).get_line()+1)
        entry.set_activates_default(True)
        dialog.get_content_area().append(entry)
        self.dialogs.add(dialog)
        def response(widget,result):
            self.dialogs.discard(widget)
            if self.alive() and result == Gtk.ResponseType.ACCEPT:
                entry.update()
                line = min(p.buffer.get_line_count()-1,max(0,entry.get_value_as_int()-1))
                _, iterator = p.buffer.get_iter_at_line(line)
                p.buffer.place_cursor(iterator)
                p.view.scroll_to_iter(iterator,0.1,True,0,0.5)
                p.view.grab_focus()
            widget.destroy()
        p.signals.connect(dialog,'response',response)
        dialog.present()

    def close(self):
        if self.closed: return
        self.closed = True
        self.cancel_search()
        self.search_settings.set_search_text(None)
        self.search_context.set_highlight(False)
        self.pane.view.set_extra_menu(None)
        self.pane.view.insert_action_group('editor',None)
        for dialog in tuple(self.dialogs): dialog.destroy()
        self.dialogs.clear()


def show_editor_settings(window):
    if window._shutting_down or window.dialog: return
    dialog = window.dialog = Gtk.Dialog(title='Editor Settings', transient_for=window, modal=True)
    dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
    dialog.add_button('Apply', Gtk.ResponseType.ACCEPT)
    grid = Gtk.Grid(column_spacing=12,row_spacing=8,margin_start=16,margin_end=16,margin_top=12,margin_bottom=12)
    fields = {}
    for row, (name,label,low,high) in enumerate([
            ('editor_font','Monospace font size',6,32),('editor_tab_width','Tab width',1,16),
            ('editor_indent_width','Indent width',1,16)]):
        grid.attach(Gtk.Label(label=label,xalign=0),0,row,1,1)
        control = Gtk.SpinButton.new_with_range(low,high,1)
        control.set_value(window.ui_settings.values[name])
        fields[name] = control
        grid.attach(control,1,row,1,1)
    for row, (name,label) in enumerate([
            ('editor_spaces','Insert spaces instead of tabs'),('editor_auto_indent','Auto indent'),
            ('editor_line_numbers','Line numbers'),('editor_current_line','Highlight current line'),
            ('editor_wrap','Word wrap'),('editor_whitespace','Show whitespace'),
            ('editor_trim','Trim trailing whitespace on save'),('editor_final_newline','Ensure final newline on save')],3):
        control = Gtk.CheckButton(label=label,active=window.ui_settings.values[name])
        fields[name] = control
        grid.attach(control,0,row,2,1)
    dialog.get_content_area().append(grid)
    def response(widget,result):
        if not window._shutting_down and result == Gtk.ResponseType.ACCEPT:
            values = {}
            for name,control in fields.items():
                if isinstance(control,Gtk.SpinButton):
                    control.update()
                    values[name] = control.get_value_as_int()
                else: values[name] = control.get_active()
            try:
                window.ui_settings.save(**values)
                window.apply_editor_font()
                for editor in window.editors:
                    if not editor._shutting_down: editor.editing.configure()
            except OSError as error: window.set_status('Editor preferences could not be saved: ' + str(error))
        window.dialog = None
        widget.destroy()
    window.signals.connect(dialog,'response',response)
    dialog.present()

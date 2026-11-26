"""Small core view host: read-only main tabs and model-driven sidebar sections."""
from gi.repository import Gtk, GtkSource, Pango
from .lifecycle import Signals, Sources


class ReadOnlyView(Gtk.Box):
    def __init__(self, device_id, owner, title, content, language):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.device_id, self.path = device_id, title
        self.backend, self.session = None, None
        self.disconnected, self.loading = False, False
        self._shutting_down = False
        self.signals = Signals()
        self.label = Gtk.Label(label=title + ' — ' + owner)
        self.status = Gtk.Label(label='Read-only · ' + owner, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=60)
        self.append(self.status)
        self.buffer = GtkSource.Buffer()
        self.buffer.set_text(content)
        manager = GtkSource.StyleSchemeManager.get_default()
        self.buffer.set_style_scheme(manager.get_scheme('Adwaita-dark') or manager.get_scheme('oblivion'))
        self.buffer.set_language(GtkSource.LanguageManager.get_default().get_language(language) if language else None)
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.add_css_class('naui-code')
        self.view.set_editable(False)
        self.view.set_monospace(True)
        self.view.set_show_line_numbers(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.view); scroll.set_vexpand(True)
        self.append(scroll)
        self.scroll = scroll

    def mark_disconnected(self, reason='Disconnected; view contents preserved.'):
        if not self._shutting_down:
            self.disconnected = True
            self.status.set_text(reason)

    def update_label(self): pass

    def close(self):
        if self._shutting_down: return
        self._shutting_down = True
        self.signals.close()


ACTION_ICONS = {'Stage Changes': 'list-add-symbolic', 'Stage All': 'list-add-symbolic',
                'Unstage Changes': 'list-remove-symbolic', 'Unstage All': 'list-remove-symbolic',
                'Open File': 'document-open-symbolic', 'Open Diff': 'edit-find-symbolic',
                'History': 'document-open-recent-symbolic', 'Refresh': 'view-refresh-symbolic',
                'Changes': 'view-list-symbolic'}


def compact_label(text):
    label = Gtk.Label(label=str(text).replace('\n', ' · ')[:8192], xalign=0,
                      ellipsize=Pango.EllipsizeMode.END, hexpand=True, max_width_chars=20)
    label.set_width_chars(1)
    label.set_tooltip_text(str(text)[:8192])
    return label


class SidebarSection(Gtk.Expander):
    def __init__(self, title):
        super().__init__()
        self.set_expanded(True)
        self.signals = Signals()
        self.sources = Sources()
        self.expansion = {}
        self.closed = False
        self.header = Gtk.Box(spacing=2)
        self.heading = compact_label(title)
        self.header.append(self.heading)
        self.set_label_widget(self.header)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_min_content_height(80); self.scroll.set_max_content_height(240)
        self.scroll.set_propagate_natural_height(True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.scroll.set_child(self.box)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.append(self.scroll)
        self.footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.footer_scroll = Gtk.ScrolledWindow()
        self.footer_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.footer_scroll.set_max_content_height(210)
        self.footer_scroll.set_propagate_natural_height(True)
        self.footer_scroll.set_child(self.footer); layout.append(self.footer_scroll)
        self.footer_scroll.set_visible(False)
        self.set_child(layout)

    def invoke(self, callback, alive, *args):
        # Row actions may rebuild this section. Defer until GTK's originating
        # gesture/button signal has returned, rather than disposing its sender.
        self.sources.idle(lambda: callback(*args) if not self.closed and alive() else None)

    def actions(self, container, choices, alive, labels=False):
        for title, callback in choices:
            button = Gtk.Button(label=title) if labels else Gtk.Button.new_from_icon_name(ACTION_ICONS.get(title, 'applications-system-symbolic'))
            button.set_has_frame(labels); button.set_tooltip_text(title)
            button.update_property([Gtk.AccessibleProperty.LABEL], [title])
            self.signals.connect(button, 'clicked', lambda *_, callback=callback: self.invoke(callback, alive))
            container.append(button)

    def menu(self, container, choices, alive):
        button = Gtk.MenuButton(icon_name='view-more-symbolic', has_frame=False)
        button.set_tooltip_text('Source Control actions')
        popover, box = Gtk.Popover(), Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for title, callback in choices:
            item = Gtk.Button(label=title, has_frame=False)
            def selected(*_, cb=callback):
                popover.popdown(); self.invoke(cb, alive)
            self.signals.connect(item, 'clicked', selected); box.append(item)
        popover.set_child(box); button.set_popover(popover); container.append(button)
        return button

    def row(self, row, parent, alive):
        if row.get('header'):
            choices = row.get('actions', ())
            self.actions(self.header, [(title, cb) for title, cb in choices if title == 'Refresh'], alive)
            overflow = [(title, cb) for title, cb in choices if title != 'Refresh'] + list(row.get('menu', ()))
            if overflow: self.menu(self.header, overflow, alive)
            return
        if 'children' in row:
            section = Gtk.Expander()
            label = compact_label(row['text'])
            label.set_tooltip_text(row.get('tooltip', row['text']))
            heading = Gtk.Box(spacing=4, hexpand=True)
            heading.append(label)
            self.actions(heading, row.get('actions', ()), alive, row.get('action_labels', False))
            section.set_label_widget(heading)
            key = row.get('id', row['text'])
            section.set_expanded(self.expansion.get(key, row.get('expanded', True)))
            self.signals.connect(section, 'notify::expanded', lambda widget, *_: self.expansion.__setitem__(key, widget.get_expanded()))
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for child in row['children']: self.row(child, box, alive)
            section.set_child(box); parent.append(section); return
        if 'primary' in row:
            button = Gtk.Button(label=row['text'], hexpand=True)
            button.add_css_class('suggested-action')
            button.set_sensitive(row.get('enabled', True))
            self.signals.connect(button, 'clicked', lambda *_: self.invoke(row['primary'], alive))
            parent.append(button); return
        content = Gtk.Box(spacing=2)
        label = compact_label(row['text']); label.set_tooltip_text(row.get('tooltip', row['text']))
        if row.get('detail') or row.get('icon'):
            title = Gtk.Box(spacing=6, hexpand=True)
            if row.get('icon'): title.append(Gtk.Image.new_from_icon_name(row['icon']))
            title.append(label)
            if row.get('detail'):
                detail = compact_label(row['detail']); detail.add_css_class('dim-label')
                detail.set_hexpand(False); title.append(detail)
            label = title
        if row.get('select'):
            item = Gtk.Button(has_frame=False, hexpand=True)
            item.set_child(label)
            self.signals.connect(item, 'clicked', lambda *_, cb=row['select']: self.invoke(cb, alive))
            content.append(item)
        else: content.append(label)
        if row.get('badge'):
            badge = Gtk.Label(label=row['badge'], margin_start=4, margin_end=4)
            colors = {'M': (55000, 46000, 30000), 'U': (30000, 52000, 33000), 'A': (30000, 52000, 33000), 'D': (60000, 30000, 30000)}
            if row['badge'] in colors:
                attributes = Pango.AttrList(); attributes.insert(Pango.attr_foreground_new(*colors[row['badge']]))
                badge.set_attributes(attributes)
            content.append(badge)
        if row.get('selected'): content.add_css_class('highlight')
        if not row.get('action_labels'):
            self.actions(content, row.get('actions', ()), alive)
        if row.get('menu'):
            button = self.menu(content, row['menu'], alive)
            gesture = Gtk.GestureClick(button=3)
            self.signals.connect(gesture, 'pressed', lambda *_: button.popup())
            content.add_controller(gesture)
        if row['text'] or 'input' not in row: parent.append(content)
        if row.get('action_labels') and row.get('actions'):
            actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                min_children_per_line=1, max_children_per_line=3,
                column_spacing=3, row_spacing=3)
            self.actions(actions, row['actions'], alive, labels=True)
            parent.append(actions)
        if 'input' in row:
            field = row['input']
            entry = Gtk.Entry(text=field.get('value', ''), placeholder_text=field.get('placeholder', ''), hexpand=True)
            entry.set_width_chars(1); entry.set_max_width_chars(20)
            # Update only plain draft state on changed; never rebuild here.
            self.signals.connect(entry, 'changed', lambda widget: field['changed'](widget.get_text()) if alive() and not self.closed else None)
            parent.append(entry)

    def update(self, rows, alive):
        if self.closed: return
        self.signals.close()
        while self.box.get_first_child(): self.box.remove(self.box.get_first_child())
        while self.footer.get_first_child(): self.footer.remove(self.footer.get_first_child())
        self.footer_scroll.set_visible(any(row.get('footer') for row in rows))
        while self.heading.get_next_sibling(): self.header.remove(self.heading.get_next_sibling())
        for row in rows: self.row(row, self.footer if row.get('footer') else self.box, alive)

    def close(self):
        if self.closed: return
        self.closed = True
        self.sources.close(); self.signals.close(); self.expansion.clear()


class BrowserView(ReadOnlyView):
    """Core model/list/detail host; plugins provide data and guarded actions."""
    def __init__(self, device_id, owner, title):
        super().__init__(device_id, owner, title, '', None)
        self.rows, self.filters, self.on_filter = [], [], None
        self.remove(self.scroll)
        toolbar = Gtk.Box(spacing=4)
        self.dropdown = Gtk.DropDown()
        self.dropdown.set_hexpand(True)
        toolbar.append(self.dropdown)
        self.refresh = Gtk.Button.new_from_icon_name('view-refresh-symbolic')
        self.refresh.set_tooltip_text('Refresh history')
        self.more = Gtk.Button(label='Load More')
        toolbar.append(self.refresh); toolbar.append(self.more)
        self.append(toolbar)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroll.set_child(self.listbox); list_scroll.set_min_content_height(120)
        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        detail.append(self.scroll)
        self.files = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        files_scroll = Gtk.ScrolledWindow()
        files_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        files_scroll.set_min_content_height(60); files_scroll.set_max_content_height(180)
        files_scroll.set_propagate_natural_height(True); files_scroll.set_child(self.files)
        detail.append(files_scroll)
        pane = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        pane.set_start_child(list_scroll); pane.set_end_child(detail)
        pane.set_position(260); pane.set_vexpand(True)
        self.append(pane)
        self.action_signals = Signals()
        self.signals.connect(self.listbox, 'row-selected', self.selected)
        self.signals.connect(self.dropdown, 'notify::selected', self.filtered)

    def selected(self, _, row):
        if self._shutting_down or self.disconnected or row is None: return
        index = row.get_index()
        if index < len(self.rows): self.rows[index]['select']()

    def filtered(self, dropdown, *_):
        index = dropdown.get_selected()
        if index < len(self.filters): dropdown.set_tooltip_text(self.filters[index][1])
        if not self._shutting_down and not self.disconnected and self.on_filter and index < len(self.filters):
            self.on_filter(self.filters[index][0])

    def set_filters(self, filters, callback):
        selected = self.dropdown.get_selected()
        previous = self.filters[selected][0] if selected < len(self.filters) else None
        self.on_filter = None
        self.filters = list(filters)
        self.dropdown.set_model(Gtk.StringList.new([label for key, label in filters]))
        self.dropdown.set_selected(next((index for index, (key, _) in enumerate(filters) if key == previous), 0))
        self.on_filter = callback

    def set_rows(self, rows):
        self.rows = []  # Removal emits row-selected; ignore obsolete selections.
        while self.listbox.get_first_child(): self.listbox.remove(self.listbox.get_first_child())
        for row in rows:
            self.listbox.append(compact_label(row['text']))
        self.rows = list(rows)

    def set_detail(self, content, files):
        self.buffer.set_text(content)
        self.action_signals.close()
        while self.files.get_first_child(): self.files.remove(self.files.get_first_child())
        for item in files:
            button = Gtk.Button()
            button.set_child(compact_label(item['text']))
            self.action_signals.connect(button, 'clicked', lambda *_, callback=item['select']:
                callback() if not self._shutting_down and not self.disconnected else None)
            self.files.append(button)

    def availability(self, connected, message):
        self.disconnected = not connected
        self.status.set_text(message[:4096])
        self.status.set_tooltip_text(message[:4096])
        for widget in (self.dropdown, self.refresh, self.more, self.files): widget.set_sensitive(connected)

    def close(self):
        if self._shutting_down: return
        self.action_signals.close()
        self.rows, self.filters, self.on_filter = [], [], None
        super().close()

"""Reusable, read-only side-by-side / inline patch view with native ownership."""
from gi.repository import Gtk, GtkSource
from .plugin_views import ReadOnlyView
from .diff_model import parse_diff, side_rows, size_mode


class DiffView(ReadOnlyView):
    keybinding_context = 'git'

    def __init__(self, device_id, owner, title, patch, *, open_file=None, context=None):
        super().__init__(device_id, owner, title, '', None)
        manager = GtkSource.StyleSchemeManager.get_default()
        scheme = manager.get_scheme('Adwaita-dark') or manager.get_scheme('oblivion')
        self.buffer.set_style_scheme(scheme)
        self.css = Gtk.CssProvider()
        css = '''
            textview.naui-diff, textview.naui-diff text { background-color: #000000; color: #d4d4d4; }
            textview.naui-diff text selection { background-color: #264f78; color: #ffffff; }
            .naui-diff-added { color: #96e6ac; }
            .naui-diff-removed { color: #ffaaaa; }
            .naui-diff-modified { color: #f2d28b; }
        '''
        if hasattr(self.css, 'load_from_string'): self.css.load_from_string(css)
        else: self.css.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.diff_context = context
        self.open_file, self.owner = open_file, owner
        self.patch, self.model = patch, None
        self.hunk_offsets, self.current_hunk = [], -1
        self.remove(self.scroll)
        self.toolbar = Gtk.Box(spacing=4)
        self.mode = Gtk.DropDown.new_from_strings(['Side by Side', 'Unified'])
        self.toolbar.append(self.mode)
        for label, callback in [('Previous Change', lambda: self.change(-1)), ('Next Change', lambda: self.change(1))]:
            button = Gtk.Button(label=label)
            self.toolbar.append(button); self.signals.connect(button, 'clicked', lambda *_, cb=callback: cb())
        self.file_button = Gtk.Button(label='Open File', sensitive=bool(open_file))
        self.signals.connect(self.file_button, 'clicked', lambda *_: self.open_file() if self.open_file and not self.disconnected and not self._shutting_down else None)
        self.toolbar.append(self.file_button); self.append(self.toolbar)
        legend = Gtk.Box(spacing=16)
        for text, kind in [('+ Added', 'added'), ('− Removed', 'removed'), ('~ Modified · before → after', 'modified')]:
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class('naui-diff-' + kind)
            legend.append(label)
        legend.set_margin_start(10); legend.set_margin_top(6); legend.set_margin_bottom(6)
        self.append(legend)
        self.left_buffer = GtkSource.Buffer()
        self.left_buffer.set_style_scheme(self.buffer.get_style_scheme())
        self.left = GtkSource.View.new_with_buffer(self.left_buffer)
        self.left.set_editable(False); self.left.set_monospace(True); self.left.add_css_class('naui-code')
        for view in (self.left, self.view):
            view.add_css_class('naui-diff')
            view.set_left_margin(10); view.set_right_margin(10)
            view.set_pixels_above_lines(2); view.set_pixels_below_lines(2)
            view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.view.set_show_line_numbers(False)  # Patch line numbers are logical, not buffer row indices.
        self.left_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.left_scroll.set_child(self.left)
        left_box, right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL), Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        info = context or {}
        self.left_title = Gtk.Label(label='Before · HEAD' if info.get('staged') else 'Before · Index')
        self.right_title = Gtk.Label(label='After · Index' if info.get('staged') else 'After · Working Tree')
        if info.get('historical'): self.left_title.set_text('Previous Version'); self.right_title.set_text('New Version')
        left_box.append(self.left_title); left_box.append(self.left_scroll)
        right_box.append(self.right_title); right_box.append(self.scroll)
        self.left_box, self.right_box = left_box, right_box
        self.pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, vexpand=True)
        self.pane.set_wide_handle(True)
        self.pane.set_start_child(left_box); self.pane.set_end_child(right_box)
        self.pane.set_resize_start_child(True); self.pane.set_resize_end_child(True)
        self._balanced = False
        self.signals.connect(self.pane, 'notify::max-position', self.balance_panes)
        self.append(self.pane)
        self._syncing = False
        for scroll, other in [(self.scroll, self.left_scroll), (self.left_scroll, self.scroll)]:
            self.signals.connect(scroll.get_vadjustment(), 'value-changed', lambda adj, other=other: self.sync_scroll(adj, other))
        self.signals.connect(self.mode, 'notify::selected', lambda *_: self.render() if self.model is not None else None)
        state = size_mode(patch)
        if state != 'normal':
            self.status.set_text('Large diff · Open File, or explicitly render within the view limit.')
            if state == 'large':
                self.anyway = Gtk.Button(label='Open Anyway')
                self.toolbar.append(self.anyway)
                self.signals.connect(self.anyway, 'clicked', lambda *_: self.load())
            self.buffer.set_text('Large diff: automatic rendering is disabled.' if state == 'large' else 'Diff exceeds the hard rendering limit. Open the file in the editor.')
        else: self.load()

    def balance_panes(self, pane, *_):
        if not self._balanced and pane.get_property('max-position') > 0:
            self._balanced = True
            pane.set_position((pane.get_property('min-position') + pane.get_property('max-position')) // 2)

    def add_hunk_action(self, callback):
        button = Gtk.Button(label='Unstage Change' if (self.diff_context or {}).get('staged') else 'Stage Change')
        button.set_tooltip_text('Use Previous / Next Change to choose the block to stage. Reopen the diff after staging.')
        self.signals.connect(button, 'clicked', lambda *_: callback() if not self.disconnected and not self._shutting_down else None)
        self.toolbar.append(button)

    def sync_scroll(self, adj, other):
        if self._shutting_down or self._syncing or self.mode.get_selected() == 1: return
        self._syncing = True
        try: other.get_vadjustment().set_value(adj.get_value())
        finally: self._syncing = False

    def load(self):
        if self._shutting_down: return
        try: self.model = parse_diff(self.patch)
        except ValueError as error:
            self.status.set_text(str(error)); self.buffer.set_text('The patch could not be rendered.'); return
        if self.model.documents: self.patch = ''  # Release the second full patch copy after parsing.
        if hasattr(self, 'anyway'): self.anyway.set_sensitive(False)
        added = removed = modified = 0
        for hunk in self.model.hunks:
            for a, b in side_rows(hunk):
                if a and b and a.kind == 'removed' and b.kind == 'added': modified += 1
                else:
                    removed += bool(a and a.kind == 'removed')
                    added += bool(b and b.kind == 'added')
        blocks = len(self.model.hunks)
        self.status.set_text(f'Read-only · {added} added · {removed} removed · {modified} modified · {blocks} change block{"s" if blocks != 1 else ""} · {self.owner}')
        self.render()

    @staticmethod
    def fill(buffer, rows, inline=False):
        buffer.remove_all_tags(buffer.get_start_iter(), buffer.get_end_iter())
        buffer.set_text('\n'.join(text for text, kind in rows))
        for kind, foreground, background in [
                ('added', '#b6f5c6', '#102b1b'), ('removed', '#ffbaba', '#351417'),
                ('modified', '#ffe3a3', '#30250f'), ('heading', '#9ecbff', '#111b27'),
                ('padding', '#666666', '#101010'), ('note', '#999999', '#000000')]:
            tag = buffer.get_tag_table().lookup('diff-' + kind)
            if tag is None: tag = buffer.create_tag('diff-' + kind)
            tag.set_property('foreground', foreground)
            tag.set_property('paragraph-background', background)
            for index, (_, row_kind) in enumerate(rows):
                if row_kind == kind:
                    buffer.apply_tag(tag, buffer.get_iter_at_line(index)[1], buffer.get_iter_at_line(index + 1)[1] if index + 1 < len(rows) else buffer.get_end_iter())
        numbers = buffer.get_tag_table().lookup('diff-numbers')
        if numbers is None: numbers = buffer.create_tag('diff-numbers', foreground='#858585')
        for index, (text, kind) in enumerate(rows):
            if kind in ('context', 'added', 'removed', 'modified'):
                start = buffer.get_iter_at_line(index)[1]
                end = start.copy(); end.forward_chars(min(len(text), 13 if inline else 6))
                buffer.apply_tag(numbers, start, end)

    def render(self):
        if self.model is None or self._shutting_down: return
        inline = self.mode.get_selected() == 1
        self.left_box.set_visible(not inline)
        self.right_title.set_text('Before / After · Unified' if inline else ('New Version' if (self.diff_context or {}).get('historical') else 'After · Index' if (self.diff_context or {}).get('staged') else 'After · Working Tree'))
        left, right = [], []
        self.hunk_offsets = []
        for doc in self.model.documents:
            left.append((doc.old_path or 'Before', 'heading'))
            right.append(((doc.old_path or 'Before') + ' → ' + (doc.new_path or 'After') if inline else doc.new_path or 'After', 'heading'))
            for text in doc.metadata:
                if not text.startswith('diff --git'):
                    left.append((text, 'heading')); right.append((text, 'heading'))
            for hunk in doc.hunks:
                self.hunk_offsets.append(len(right))
                title = f'Lines {hunk.old_start} → {hunk.new_start}' + (' · ' + hunk.heading if hunk.heading else '')
                left.append((title, 'heading')); right.append((title, 'heading'))
                pairs = side_rows(hunk)
                modified = {id(line) for a, b in pairs if a and b and a.kind == 'removed' and b.kind == 'added' for line in (a, b)}
                if inline:
                    for line in hunk.lines:
                        a, b = str(line.old_number or ''), str(line.new_number or '')
                        prefix = '+' if line.kind == 'added' else '-' if line.kind == 'removed' else ' '
                        changed = id(line) in modified
                        right.append((f'{a:>6} {b:>6} {"~" if changed else " "}{prefix} {line.text}', 'modified' if changed else line.kind))
                else:
                    for a, b in pairs:
                        for line, rows, number in [(a, left, a.old_number if a else None), (b, right, b.new_number if b else None)]:
                            if line is None:
                                rows.append(('', 'padding')); continue
                            changed = id(line) in modified
                            prefix = '+' if line.kind == 'added' else '-' if line.kind == 'removed' else ' '
                            rows.append((f'{str(number or ""):>6} {"~" if changed else " "}{prefix} {line.text}', 'modified' if changed else line.kind))
        if not right: right = [('No textual changes.', 'context')]
        if not self.model.documents: right = [(self.patch or 'No textual changes.', 'context')]
        self.fill(self.left_buffer, left); self.fill(self.buffer, right, inline)
        self.current_hunk = -1

    def change(self, direction):
        if self._shutting_down or not self.hunk_offsets: return
        self.current_hunk = (0 if direction > 0 else len(self.hunk_offsets) - 1) if self.current_hunk < 0 else (self.current_hunk + direction) % len(self.hunk_offsets)
        line = self.hunk_offsets[self.current_hunk]
        self.view.scroll_to_iter(self.buffer.get_iter_at_line(line)[1], 0.15, True, 0, 0.3)
        if self.mode.get_selected() == 0:
            self.left.scroll_to_iter(self.left_buffer.get_iter_at_line(line)[1], 0.15, True, 0, 0.3)

    def mark_disconnected(self, reason='Disconnected; diff contents preserved.'):
        super().mark_disconnected(reason)
        self.file_button.set_sensitive(False)

    def close(self):
        if self._shutting_down: return
        Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.css)
        self.open_file, self.diff_context, self.model, self.patch = None, None, None, ''
        super().close()

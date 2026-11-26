"""Targeted tab context actions with deferred native-widget disposal."""
from gi.repository import Gtk, Gdk
from .lifecycle import Signals, Sources


class TabMenu:
    def __init__(self, window):
        self.window = window
        self.signals, self.sources = Signals(), Sources()
        self.popover = None
        self.page = None
        self.closed = False

    def attach(self, tab, page, signals):
        gesture = Gtk.GestureClick(button=3)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        def pressed(gesture, count, x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.show(tab, page, x, y)
        signals.connect(gesture, 'pressed', pressed)
        tab.add_controller(gesture)

    def dismiss(self):
        self.page = None
        self.signals.close()
        if self.popover:
            self.popover.popdown()
            self.popover.unparent()
            self.popover = None

    def actions(self, page):
        w, tabs = self.window, self.window.notebook
        editor = page in w.editors
        actions = []
        if editor:
            actions += [('Save', page.save, not page.loading and not page.saving and not page.disconnected),
                        ('Save a Copy…', lambda: w.save_copy(page), not page.loading)]
        actions += [('Keep Open', lambda: tabs.keep_open(page), page in tabs.previews),
                    ('Unpin Tab' if page in tabs.pinned else 'Pin Tab',
                     lambda: tabs.set_pinned(page, page not in tabs.pinned), True),
                    ('Split Side by Side', lambda: self.split(page, Gtk.Orientation.HORIZONTAL), len(tabs.pages)>1),
                    ('Split Top and Bottom', lambda: self.split(page, Gtk.Orientation.VERTICAL), len(tabs.pages)>1),
                    ('Move to Other Group', lambda: self.move(page), len(tabs.groups)>1)]
        from .plugin_views import ReadOnlyView
        if editor or isinstance(page, ReadOnlyView):
            actions.append(('Duplicate Tab as Snapshot', lambda: self.duplicate(page), not getattr(page,'loading',False)))
        if getattr(page, 'backend', None) and getattr(page, 'path', '').startswith('/'):
            actions.append(('Copy File Path', lambda: w.get_clipboard().set(page.path), True))
        actions.append(('Close Tab', lambda: w.close_viewer(page), not getattr(page,'saving',False)))
        return actions

    def show(self, tab, page, x, y):
        if self.closed or page not in self.window.viewers: return
        self.dismiss()
        self.page = page
        popover = self.popover = Gtk.Popover()
        popover.set_parent(tab)
        rect = Gdk.Rectangle();rect.x=int(x);rect.y=int(y);rect.width=rect.height=1
        popover.set_pointing_to(rect)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for title, callback, enabled in self.actions(page):
            if title in {'Keep Open', 'Split Side by Side', 'Close Tab'} and box.get_first_child():
                box.append(Gtk.Separator())
            button = Gtk.Button(label=title, has_frame=False, sensitive=enabled)
            if title == 'Duplicate Tab as Snapshot':
                button.set_tooltip_text('Read-only copy of the current contents, including unsaved text. The original stays editable.')
            def clicked(*_, callback=callback):
                popover.popdown()
                def invoke():
                    if self.closed: return
                    self.dismiss()
                    if page in self.window.viewers: callback()
                self.sources.timeout(0, invoke)
            self.signals.connect(button, 'clicked', clicked);box.append(button)
        popover.set_child(box);popover.popup()

    def split(self, page, orientation):
        self.window.notebook.keep_open(page)
        self.window.notebook.set_current_page(self.window.notebook.page_num(page))
        self.window.split_tabs(orientation)

    def move(self, page):
        tabs = self.window.notebook
        tabs.set_current_page(tabs.page_num(page));tabs.move_current()

    def duplicate(self, page):
        w = self.window
        w.notebook.keep_open(page)
        w.notebook.set_current_page(w.notebook.page_num(page))
        from .diff_view import DiffView
        if isinstance(page, DiffView):
            copy = w.open_diff_view(page.device_id, page.path, page.patch,
                open_file=page.open_file, context=dict(page.diff_context or {}))
        else:
            buffer = page.buffer
            content = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            language = buffer.get_language()
            copy = w.open_text_view(page.device_id, 'Snapshot: ' + page.path, content,
                                    language.get_id() if language else None)
        if copy:
            w.notebook.keep_open(copy)
            w.set_status('Snapshot duplicated. The original remains editable; the snapshot is read-only.')
        return copy

    def close(self):
        self.closed = True
        self.sources.close();self.dismiss()

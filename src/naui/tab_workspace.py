"""Two native tab groups with a stable, window-wide page interface.

Moving a tab reparents its existing widget; it never closes a document or PTY.
"""
from gi.repository import GObject, Gtk, Pango

from .lifecycle import Signals, Sources


class TabWorkspace(Gtk.Box):
    __gsignals__ = {'switch-page': (GObject.SignalFlags.RUN_LAST, None, (Gtk.Widget, int))}

    def __init__(self, settings=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.settings, self.geometry = settings, None
        self.signals = Signals()
        self.sources = Sources()
        self.pinned, self.recent = set(), []
        self.previews = set()
        self.pending_preview_closes = set()
        self.cycle_order = None
        self.cycle_position = 0
        self.group_name = "naui-" + str(id(self))
        self.pages, self.groups = [], []
        self.current, self.paned = None, None
        self.changing = False
        self.active = self._new_group()
        self.append(self.active)

    def _new_group(self):
        group = Gtk.Notebook(scrollable=True, hexpand=True, vexpand=True)
        group.add_css_class('naui-tabs')
        group.set_group_name(self.group_name)
        badge = Gtk.Label(label='', margin_start=6, margin_end=6)
        group.set_action_widget(badge, Gtk.PackType.END)
        self.signals.connect(group, 'page-added', self._page_added)
        self.signals.connect(group, 'page-reordered', self._reordered)
        self.signals.connect(group, 'switch-page', self._switched)
        focus = Gtk.EventControllerFocus()
        self.signals.connect(focus, 'enter', lambda *_: self._activate(group))
        group.add_controller(focus)
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.signals.connect(click, 'pressed', lambda *_: self._activate(group))
        group.add_controller(click)
        self.groups.append(group)
        return group

    def _page_added(self, group, page, position):
        group.set_tab_reorderable(page, True)
        group.set_tab_detachable(page, True)
        if not self.changing:
            self.sources.idle(lambda: self._settle_drag(group, page))

    def _settle_drag(self, group, page):
        if group not in self.groups or page not in self.pages or self.owner(page) is not group: return
        # Dragging a tab explicitly keeps it open.
        if page in self.previews and any(other is not page and other not in self.pending_preview_closes and self.owner(other) is group for other in self.previews):
            self.keep_open(page)
        self._reordered(group, page, 0)
        self._activate(group)
        if len(self.groups) == 2 and any(not item.get_n_pages() for item in self.groups): self.single_view()

    def _reordered(self, *_):
        if self.changing: return
        order = [group.get_nth_page(i) for group in self.groups for i in range(group.get_n_pages())]
        if set(order) == set(self.pages): self.pages[:] = order

    def set_pinned(self, page, value):
        if page not in self.pages: return
        if value:
            self.keep_open(page)
            self.pinned.add(page)
        else: self.pinned.discard(page)
        close = getattr(page, 'tab_close_button', None)
        if close: close.set_visible(not value)
        group = self.owner(page)
        tab = group.get_tab_label(page)
        if not hasattr(tab, 'pin_indicator'):
            tab.pin_indicator = Gtk.Image.new_from_icon_name('view-pin-symbolic')
            tab.prepend(tab.pin_indicator)
        tab.pin_indicator.set_visible(value)
        if value: tab.add_css_class('naui-pinned-tab')
        else: tab.remove_css_class('naui-pinned-tab')
        tab.set_tooltip_text('Pinned tab — use Command Palette to unpin' if value else None)
        if value: group.reorder_child(page, 0)

    def keep_open(self, page):
        self.previews.discard(page)
        self.pending_preview_closes.discard(page)
        label = getattr(page, 'label', None)
        if label is not None: label.set_attributes(None)

    def preview(self, page):
        if page in self.pinned or getattr(page, 'dirty', False): return
        self.previews.add(page)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_style_new(Pango.Style.ITALIC))
        page.label.set_attributes(attrs)

    def toggle_pin(self):
        if self.current: self.set_pinned(self.current, self.current not in self.pinned)

    def recent_tab(self, reverse=False):
        if self.cycle_order is None:
            self.cycle_order = [page for page in self.recent if page in self.pages]
            self.cycle_position = 0
        self.cycle_order = [page for page in self.cycle_order if page in self.pages]
        if not self.cycle_order: return
        self.cycle_position = (self.cycle_position + (-1 if reverse else 1)) % len(self.cycle_order)
        self.set_current_page(self.page_num(self.cycle_order[self.cycle_position]))

    def end_cycle(self):
        self.cycle_order = None

    def _switched(self, group, page, _position):
        if not self.changing: self._activate(group, page)

    def _activate(self, group, page=None):
        if self.changing or group not in self.groups: return
        page = page or group.get_nth_page(group.get_current_page())
        if page not in self.pages: return
        changed = self.current is not page or self.active is not group
        self.active, self.current = group, page
        if page in self.recent: self.recent.remove(page)
        self.recent.insert(0, page)
        badge = group.get_action_widget(Gtk.PackType.END)
        name = getattr(page, "device_label", getattr(page, "device_id", ""))
        root = self.get_root()
        workspace = getattr(root, "workspace", None)
        device = workspace.devices.get(getattr(page, "device_id", None)) if workspace else None
        if device: name = device.display_name
        badge.set_text(name[:32])
        badge.set_tooltip_text(name)
        for item in self.groups:
            if item is group: item.add_css_class('naui-active-group')
            else: item.remove_css_class('naui-active-group')
        if changed: self.emit('switch-page', page, self.page_num(page))

    def owner(self, page):
        return next((group for group in self.groups if group.page_num(page) >= 0), None)

    def append_page(self, page, tab):
        self.pages.append(page)
        self.active.append_page(page, tab)
        return self.page_num(page)

    def page_num(self, page):
        return self.pages.index(page) if page in self.pages else -1

    def get_n_pages(self): return len(self.pages)

    def get_current_page(self): return self.page_num(self.current)

    def get_nth_page(self, index):
        if index == -1: index = len(self.pages) - 1
        return self.pages[index] if 0 <= index < len(self.pages) else None

    def set_current_page(self, index):
        page = self.get_nth_page(index)
        if page is None: return
        group = self.owner(page)
        group.set_current_page(group.page_num(page))
        self._activate(group, page)
        self._focus(page)

    @staticmethod
    def _focus(page):
        view = getattr(page, 'view', None)
        if isinstance(view, Gtk.Widget): view.grab_focus()
        else: page.child_focus(Gtk.DirectionType.TAB_FORWARD)

    def _clear_focus(self):
        root = self.get_root()
        if isinstance(root, Gtk.Window): root.set_focus(None)

    def remove_page(self, index):
        page = self.get_nth_page(index)
        if page is None: return
        group = self.owner(page)
        was_current = page is self.current
        self.changing = True
        try:
            self._clear_focus()
            self.pages.remove(page)
            self.pinned.discard(page)
            self.previews.discard(page)
            self.pending_preview_closes.discard(page)
            if page in self.recent: self.recent.remove(page)
            group.remove_page(group.page_num(page))
        finally: self.changing = False
        if len(self.groups) == 2 and any(not item.get_n_pages() for item in self.groups):
            self.single_view()
        elif was_current:
            self.current = None
            self._activate(group)
            if self.current: self._focus(self.current)
        elif self.current:
            self._focus(self.current)

    def _transfer(self, page, destination):
        self.keep_open(page)
        source = self.owner(page)
        tab = source.get_tab_label(page)
        source.remove_page(source.page_num(page))
        destination.append_page(page, tab)
        destination.set_current_page(destination.page_num(page))

    def split(self, orientation):
        if len(self.pages) < 2: return False
        if self.paned:
            ratio = self.settings.values['split_ratio'] if self.settings else None
            self.paned.set_orientation(orientation)
            if self.settings: self.settings.values['split_ratio'] = ratio
        else:
            page, first = self.current, self.active
            self.changing = True
            try:
                self._clear_focus()
                second = self._new_group()
                self.remove(first)
                self.paned = Gtk.Paned(orientation=orientation, wide_handle=True,
                                       hexpand=True, vexpand=True)
                self.paned.set_start_child(first)
                self.paned.set_end_child(second)
                self.paned.set_resize_start_child(True)
                self.paned.set_resize_end_child(True)
                self.paned.set_shrink_start_child(False)
                self.paned.set_shrink_end_child(False)
                if self.settings:
                    from .pane_geometry import PaneGeometry
                    self.geometry = PaneGeometry(self.paned, self.signals, self.settings)
                    self.signals.connect(self.paned, "notify::position", self.queue_geometry_save)
                self.append(self.paned)
                self._transfer(page, second)
            finally: self.changing = False
            self._activate(second, page)
            self._focus(page)
        extent = self.get_width() if orientation == Gtk.Orientation.HORIZONTAL else self.get_height()
        if self.geometry: self.geometry.restore()
        else: self.paned.set_position(max(1, extent // 2))
        return True

    def queue_geometry_save(self, *_):
        root = self.get_root()
        if not hasattr(root, 'save_ui'): return
        self.sources.remove(getattr(self, 'geometry_save_source', None))
        self.geometry_save_source = self.sources.timeout(400, root.save_ui)

    def move_current(self):
        if not self.paned or self.current is None: return False
        page = self.current
        destination = next(group for group in self.groups if group is not self.active)
        self.changing = True
        try:
            self._clear_focus()
            self._transfer(page, destination)
        finally: self.changing = False
        self._activate(destination, page)
        if any(not group.get_n_pages() for group in self.groups): self.single_view()
        self._focus(page)
        return True

    def single_view(self):
        if not self.paned: return
        page = self.current if self.current in self.pages else (self.pages[0] if self.pages else None)
        first, second = self.groups
        self.changing = True
        try:
            self._clear_focus()
            for item in self.pages:
                if self.owner(item) is second: self._transfer(item, first)
            # Restore the original tab order after moves between groups.
            for index, item in enumerate(self.pages): first.reorder_child(item, index)
            self.signals.disconnect(self.paned)
            self.paned.set_start_child(None)
            self.paned.set_end_child(None)
            self.remove(self.paned)
            self.paned = None
            self.geometry = None
            self.signals.disconnect(second)
            # Controllers belong to the discarded group; disconnect their handlers too.
            controllers = second.observe_controllers()
            for index in range(controllers.get_n_items()):
                self.signals.disconnect(controllers.get_item(index))
            self.groups.remove(second)
            self.append(first)
            self.active = first
            if page: first.set_current_page(first.page_num(page))
        finally: self.changing = False
        self.current = None
        self._activate(first, page)
        if page: self._focus(page)

    def close(self):
        self.sources.close()
        self.signals.close()

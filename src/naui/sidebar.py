"""Persistent native activity panels. Own navigation, never devices or sessions."""
from gi.repository import Gtk, Pango

from .lifecycle import Signals
from .sidebar_layout import Panel, SidebarState


ACTIVITIES = (
    Panel('explorer', 'Explorer', 'folder-symbolic', 'view.explorer'),
    Panel('search', 'Search', 'edit-find-symbolic', 'view.search'),
    Panel('source_control', 'Source Control', 'view-list-symbolic', 'view.source_control'),
    Panel('transfers', 'Transfers', 'folder-download-symbolic', 'view.transfers'),
    Panel('devices', 'Devices', 'computer-symbolic', 'view.devices'),
    Panel('settings', 'Settings', 'preferences-system-symbolic', 'view.settings'),
)


class SidebarManager(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.window, self.closed = window, False
        self.signals = Signals()
        self.state = SidebarState(window.ui_settings)
        self.buttons, self.badge_labels, self.images, self.widgets = {}, {}, {}, {}
        self.add_css_class('naui-sidebar')
        self.rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.rail.add_css_class('naui-activity-rail')
        self.rail_scroll = Gtk.ScrolledWindow()
        self.rail_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.rail_scroll.set_child(self.rail)
        self.rail_scroll.set_size_request(42, -1)
        self.append(self.rail_scroll)
        self.stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False, hexpand=True, vexpand=True)
        self.stack.add_css_class('naui-sidebar-panel')
        self.append(self.stack)
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_style()

    def register_panel(self, panel, widget):
        self.state.register(panel)
        self.widgets[panel.id] = widget
        widget.set_vexpand(True)
        self.stack.add_named(widget, panel.id)
        button = Gtk.ToggleButton(has_frame=False, tooltip_text=panel.title)
        button.update_property([Gtk.AccessibleProperty.LABEL], [panel.title])
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        image = Gtk.Image.new_from_icon_name(panel.icon)
        image.set_pixel_size(round(20 * self.window.ui_settings.values['sidebar_scale'] / 100))
        content.append(image)
        badge = Gtk.Label(visible=False)
        badge.add_css_class('caption')
        content.append(badge); button.set_child(content)
        self.buttons[panel.id], self.badge_labels[panel.id], self.images[panel.id] = button, badge, image
        self.signals.connect(button, 'clicked', lambda *_: self.select(panel.id))
        self.rail.append(button)
        self._display()
        self.update_tooltips()

    def replace_panel(self, identity, widget):
        """Replace placeholders, not live trees on every activity switch."""
        self.stack.remove(self.widgets[identity])
        self.widgets[identity] = widget
        widget.set_vexpand(True); self.stack.add_named(widget, identity)
        self._display()

    def remove_panel(self, identity):
        if self.closed or identity not in self.widgets: return
        self.state.remove(identity)
        self.stack.remove(self.widgets.pop(identity))
        button = self.buttons.pop(identity)
        self.signals.disconnect(button); self.rail.remove(button)
        self.badge_labels.pop(identity); self.images.pop(identity)
        self._display()

    def _display(self):
        if self.state.active:
            self.stack.set_visible_child_name(self.state.active)
        for identity, button in self.buttons.items(): button.set_active(identity == self.state.active)

    def select(self, identity, focus=False):
        if self.closed: return
        self.state.select(identity)
        if self.window.sidebar_collapsed: self.window.toggle_sidebar()
        self._display()
        if focus: self.focus()
        self.window.save_ui()

    def focus(self):
        widget = self.widgets.get(self.state.active)
        if widget: widget.child_focus(Gtk.DirectionType.TAB_FORWARD)

    def set_badge(self, identity, count=0, failed=False):
        if self.closed: return
        self.state.badge(identity, count, failed)
        badge = self.badge_labels[identity]
        badge.set_text(('99+' if count > 99 else str(count)) if count else ('!' if failed else ''))
        badge.set_visible(bool(count or failed))
        if failed: self.buttons[identity].add_css_class('error')
        else: self.buttons[identity].remove_css_class('error')
        self.update_tooltips()

    def update_tooltips(self):
        for identity, panel in self.state.panels.items():
            binding = self.window.command_registry.binding(panel.command) if panel.command else None
            count, failed = self.state.badges.get(identity, (0, False))
            hint = (' — ' + str(count) + (' active' if identity == 'transfers' else ' changes')) if count else ''
            if failed: hint += ' — Failed transfers'
            self.buttons[identity].set_tooltip_text(panel.title + (' (' + binding + ')' if binding else '') + hint)

    def apply_style(self):
        scale = self.window.ui_settings.values['sidebar_scale'] / 100
        padding = round((3 if self.window.ui_settings.values['sidebar_density'] == 'compact' else 6) * scale)
        # Scope to the sidebar. Text/editor/terminal font preferences are untouched.
        css = (f'.naui-sidebar {{ font-size: {round(10 * scale, 2)}pt; }}'
               f'.naui-sidebar-panel image {{ -gtk-icon-size: {round(16 * scale)}px; }}'
               f'.naui-sidebar-panel listview > row {{ padding: {padding}px 0; }}'
               f'.naui-sidebar-panel button {{ padding: {padding}px; }}'
               '.naui-activity-rail { background-color: alpha(@theme_fg_color, 0.035); border-right: 1px solid alpha(@theme_fg_color, 0.12); }'
               '.naui-activity-rail button:checked { background-color: alpha(@theme_selected_bg_color, 0.18); box-shadow: inset 3px 0 @theme_selected_bg_color; }'
               '.naui-activity-rail button { min-width: 28px; min-height: 32px; padding: 3px; border-radius: 0; }')
        if hasattr(self.css, 'load_from_string'): self.css.load_from_string(css)
        else: self.css.load_from_data(css.encode())
        for image in self.images.values(): image.set_pixel_size(round(20 * scale))
        self.rail_scroll.set_size_request(max(42, round(42 * scale)), -1)
        workspace = getattr(self.window, 'workspace_sidebar', None)
        if workspace:
            for explorer in workspace.explorers.values(): explorer.apply_sidebar_scale(scale)

    def scale(self, value):
        if self.closed: return
        previous = self.window.ui_settings.values['sidebar_scale']
        self.state.scale(value)
        if previous == value: return
        control = getattr(self, 'scale_control', None)
        if control and control.get_value_as_int() != value: control.set_value(value)
        self.apply_style(); self.window.save_ui()

    def zoom(self, direction):
        self.scale(100 if direction == 0 else max(80, min(150, self.window.ui_settings.values['sidebar_scale'] + direction * 5)))

    def close(self):
        if self.closed: return
        self.closed = True
        self.signals.close(); self.state.close()
        Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.css)


def empty_panel(title, message):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    heading = Gtk.Label(label=title.upper(), xalign=0, ellipsize=Pango.EllipsizeMode.END)
    heading.add_css_class('heading'); box.append(heading)
    label = Gtk.Label(label=message, wrap=True, xalign=0, max_width_chars=20)
    box.append(label)
    return box


def settings_panel(window, manager):
    from .editor_view import show_editor_settings
    box = empty_panel('Settings', 'Sidebar appearance')
    scale = Gtk.SpinButton.new_with_range(80, 150, 5)
    scale.set_value(window.ui_settings.values['sidebar_scale'])
    scale.set_tooltip_text('Sidebar scale (%) — independent of editor and terminal zoom')
    scale.update_property([Gtk.AccessibleProperty.LABEL], ['Sidebar scale (%)'])
    manager.signals.connect(scale, 'value-changed', lambda widget: manager.scale(widget.get_value_as_int()))
    box.append(scale)
    density = Gtk.ComboBoxText()
    for name in ('Compact', 'Comfortable'): density.append_text(name)
    density.set_active(0 if window.ui_settings.values['sidebar_density'] == 'compact' else 1)
    density.update_property([Gtk.AccessibleProperty.LABEL], ['Sidebar density'])
    def change_density(widget):
        window.ui_settings.values['sidebar_density'] = 'compact' if widget.get_active() == 0 else 'comfortable'
        manager.apply_style(); window.save_ui()
    manager.signals.connect(density, 'changed', change_density); box.append(density)
    # Existing settings and tools remain in their established main views/dialogs.
    for title, callback in [('Editor Settings…', lambda: show_editor_settings(window)),
                            ('Keybindings…', window.show_keybindings), ('Plugins…', window.show_plugins),
                            ('Reset Layout', window.reset_layout)]:
        button = Gtk.Button(label=title)
        button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        button.get_child().set_width_chars(1)
        manager.signals.connect(button, 'clicked', lambda *_, cb=callback: window.sources.idle(cb))
        box.append(button)
    manager.scale_control, manager.density_control = scale, density
    scroll = Gtk.ScrolledWindow(vexpand=True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_child(box)
    return scroll

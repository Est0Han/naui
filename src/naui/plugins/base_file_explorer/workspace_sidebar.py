"""A persistent panel per device; selection never destroys another device tree."""
import os

from gi.repository import Gtk, Pango

from .explorer import ExplorerSidebar
from ...lifecycle import Signals
from ...navigation import navigation_path
from ...workspace import ssh_config_hosts, ssh_host
from ...sidebar_layout import root_label, workspace_header


class WorkspaceSidebar(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.window = window
        self._shutting_down = False
        self.signals = Signals()
        self.explorers, self.cards, self.connection_buttons, self.device_rows = {}, {}, {}, {}
        self.set_size_request(0, -1)
        devices_section = Gtk.Expander(label='DEVICES', expanded=window.ui_settings.values['devices_expanded'])
        self.devices_section = devices_section
        self.device_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        device_scroll = Gtk.ScrolledWindow()
        device_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        device_scroll.set_vexpand(True)
        devices_section.set_vexpand(True)
        device_scroll.set_child(self.device_list); devices_section.set_child(device_scroll)
        # Device management is hosted independently by the activity stack.
        self.devices_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        device_toolbar = Gtk.Box(spacing=2)
        device_toolbar.append(Gtk.Label(label='DEVICES', xalign=0, hexpand=True))
        add_device = Gtk.Button(icon_name='list-add-symbolic', has_frame=False, tooltip_text='Add Device')
        self.signals.connect(add_device, 'clicked', lambda *_: self.add_device_dialog())
        device_toolbar.append(add_device)
        self.devices_panel.append(device_toolbar)
        devices_section.set_label('Connections')
        self.devices_panel.append(devices_section)
        toolbar = Gtk.Box(spacing=2)
        heading = Gtk.Label(label='EXPLORER', xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        heading.add_css_class('heading')
        toolbar.append(heading)
        self.file_button = Gtk.Button.new_from_icon_name('document-new-symbolic')
        self.folder_button = Gtk.Button.new_from_icon_name('folder-new-symbolic')
        self.refresh_button = Gtk.Button.new_from_icon_name('view-refresh-symbolic')
        for button, tooltip, action in ((self.file_button, 'New File', lambda: self.new_item(False)),
                (self.folder_button, 'New Folder', lambda: self.new_item(True)),
                (self.refresh_button, 'Refresh selected device', lambda: self.active_explorer.refresh())):
            button.set_has_frame(False)
            button.set_tooltip_text(tooltip)
            self.signals.connect(button, 'clicked', lambda *_, action=action: action())
            # Actions are reachable through the compact create menu below.
        create = Gtk.MenuButton(icon_name='list-add-symbolic', tooltip_text='Create / Add Root', has_frame=False)
        create_popover = Gtk.Popover()
        create_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for control in (self.file_button, self.folder_button):
            control.set_child(Gtk.Label(label='New File' if control is self.file_button else 'New Folder'))
            self.signals.connect(control, 'clicked', lambda *_: create_popover.popdown())
            create_actions.append(control)
        add_root = Gtk.Button(label='Add Workspace Root', has_frame=False)
        self.signals.connect(add_root, 'clicked', lambda *_: (create_popover.popdown(), self.root_dialog(self.window.workspace.active_device_id)))
        create_actions.append(add_root)
        create_popover.set_child(create_actions); create.set_popover(create_popover); toolbar.append(create)
        self.device_button = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text='Workspace actions')
        popover = Gtk.Popover()
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for text, action in [('Add Local Root', lambda: self.root_dialog('local')),
                             ('Add SSH Device', self.add_device_dialog),
                             ('Collapse All', self.collapse_all), ('Refresh All', self.refresh_all),
                             ('Reset Sidebar Layout', self.window.reset_layout),
                             ('Explorer Settings', lambda: self.window.show_sidebar_panel('settings')),
                             ('ls after cd', lambda: self.active_explorer.run_ls.set_active(not self.active_explorer.run_ls.get_active()))]:
            button = Gtk.Button(label=text)
            self.signals.connect(button, 'clicked', lambda *_, action=action: (popover.popdown(), action()))
            actions.append(button)
        actions.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.hide_disconnected = Gtk.CheckButton(label='Hide disconnected devices')
        self.hide_disconnected.set_active(window.ui_settings.values['explorer_hide_disconnected'])
        self.hide_disconnected.set_tooltip_text('Hide their Explorer cards. All connections remain available in Devices.')
        self.signals.connect(self.hide_disconnected, 'toggled', self.change_device_visibility)
        actions.append(self.hide_disconnected)
        height_row = Gtk.Box(spacing=6)
        height_row.append(Gtk.Label(label='Maximum tree height', xalign=0, hexpand=True))
        self.tree_height = Gtk.SpinButton.new_with_range(40, 600, 20)
        self.tree_height.set_value(window.ui_settings.values['explorer_tree_height'])
        self.tree_height.set_tooltip_text('Maximum height per device tree in pixels; short trees shrink to their contents.')
        self.tree_height.update_property([Gtk.AccessibleProperty.LABEL], ['Maximum tree height'])
        self.signals.connect(self.tree_height, 'value-changed', self.change_tree_height)
        height_row.append(self.tree_height)
        actions.append(height_row)
        popover.set_child(actions); self.device_button.set_popover(popover)
        toolbar.append(self.device_button)
        self.files_section = Gtk.Expander(expanded=window.ui_settings.values['files_expanded'])
        self.files_section.set_label_widget(toolbar)
        self.files_section.set_vexpand(True)
        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, vexpand=True)
        # Only this spacer consumes spare height. Collapsed cards stay together
        # at the bottom without stretching their rows or recreating live trees.
        self.card_spacer = Gtk.Box(vexpand=True)
        self.container.append(self.card_spacer)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll = scroll
        scroll.set_vexpand(True)
        scroll.set_child(self.container)
        self.files_section.set_child(scroll)
        self.append(self.files_section)
        for section, name in ((devices_section, 'devices_expanded'), (self.files_section, 'files_expanded')):
            self.signals.connect(section, 'notify::expanded', lambda section, _, name=name:
                self.window.ui_settings.values.update({name: section.get_expanded()}))
        for device in window.workspace.devices.values():
            self.add_device(device)

    @property
    def active_explorer(self):
        return self.explorers[self.window.workspace.active_device_id]

    def add_device(self, device):
        device_button = Gtk.Button(label=device.display_name)
        device_button.set_has_frame(False)
        device_button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        device_button.get_child().set_max_width_chars(22)
        self.signals.connect(device_button, 'clicked', lambda *_: self.window.activate_device(device.id))
        device_row = Gtk.Box(spacing=3)
        device_row.append(Gtk.Image())
        device_button.set_hexpand(True)
        device_row.append(device_button)
        self.device_list.append(device_row)
        self.device_rows[device.id] = device_row
        expander = Gtk.Expander()
        expanded = self.window.ui_settings.values['explorer_device_expanded']
        expander.set_expanded(expanded.get(device.id, device.connected))
        expander.set_vexpand(False)
        expander.set_valign(Gtk.Align.START)
        self.signals.connect(expander, 'notify::expanded', lambda card, *_: self.remember_expansion(device.id, card))
        header = Gtk.Box(spacing=5)
        icon, status = Gtk.Image(), Gtk.Label()
        select = Gtk.Button(label=device.display_name)
        select.set_has_frame(False)
        select.set_hexpand(True)
        select.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        select.get_child().set_max_width_chars(18)
        self.signals.connect(select, "clicked", lambda *_: self.window.activate_device(device.id))
        # Device actions live in one menu, not in the filesystem card header.
        status.set_visible(False)
        header.append(icon); header.append(select)
        menu = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text='Device actions')
        popover = Gtk.Popover()
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        choices = [('Open Terminal', lambda: self.window.activate_device(device.id)),
                   ('New Terminal', lambda: self.window.connect_device(device, new_terminal=True)
                    if device.type == 'ssh' else self.window.create_terminal(device, device.navigation_state.current_path)),
                   ('Add Root', lambda: self.root_dialog(device.id))]
        if device.type == 'ssh':
            choices += [('Reconnect', lambda: self.window.activate_device(device.id)),
                        ('Disconnect', lambda: self.disconnect(device.id)),
                        ('Properties', lambda: self.window.device_properties(device.id)),
                        ('Remove from Workspace', lambda: self.window.remove_device_dialog(device.id))]
        for label, callback in choices:
            control = Gtk.Button(label=label)
            self.signals.connect(control, 'clicked', lambda *_, callback=callback: (popover.popdown(), callback()))
            actions.append(control)
        popover.set_child(actions); menu.set_popover(popover); device_row.append(menu)
        expander.set_label_widget(header)
        explorer = ExplorerSidebar(self.window, device)
        explorer.set_context(device.filesystem_backend, device.session_id,
                             device.navigation_state.current_path, device.navigation_state.home, device.state)
        explorer.set_vexpand(False)
        expander.set_child(explorer)
        self.container.append(expander)
        self.explorers[device.id] = explorer
        self.cards[device.id] = (expander, icon, select, status)
        self.update_devices()

    def remember_expansion(self, identity, card):
        if self._shutting_down: return
        expanded = dict(self.window.ui_settings.values['explorer_device_expanded'])
        # Drop old devices so preferences remain bounded by workspace size.
        expanded = {key: value for key, value in expanded.items() if key in self.window.workspace.devices}
        expanded[identity] = card.get_expanded()
        self.window.ui_settings.values['explorer_device_expanded'] = expanded
        self.arrange_cards()
        self.window.save_ui()

    def arrange_cards(self):
        cards = [card[0] for card in self.cards.values()]
        ordered = ([card for card in cards if card.get_expanded()] + [self.card_spacer]
                   + [card for card in cards if not card.get_expanded()])
        previous = None
        for card in ordered:
            self.container.reorder_child_after(card, previous)
            previous = card

    def change_device_visibility(self, control):
        if self._shutting_down: return
        self.window.ui_settings.values['explorer_hide_disconnected'] = control.get_active()
        self.update_devices()
        self.window.save_ui()

    def change_tree_height(self, control):
        if self._shutting_down: return
        height = control.get_value_as_int()
        self.window.ui_settings.values['explorer_tree_height'] = height
        self.window.ui_settings.values["explorer_device_heights"] = {}
        for explorer in self.explorers.values(): explorer.apply_tree_size()
        self.window.save_ui()

    def disconnect(self, identity):
        device = self.window.workspace.devices[identity]
        if device.type == 'ssh' and device.state != 'disconnected':
            self.window.confirm_action('Disconnect ' + device.display_name + '?',
                'End its Naui SSH terminals and cancel its transfers. The remote machine remains running; unsaved editor contents remain open.',
                'Disconnect', lambda: self.window.disconnect_device(identity))
        elif device.type == 'ssh':
            self.window.activate_device(identity)

    def remove_device(self, identity):
        explorer = self.explorers.pop(identity)
        explorer.close()
        expander = self.cards.pop(identity)[0]
        row = self.device_rows.pop(identity)
        def disconnect_tree(widget):
            self.signals.disconnect(widget)
            child = widget.get_first_child()
            while child:
                disconnect_tree(child)
                child = child.get_next_sibling()
        disconnect_tree(expander)
        disconnect_tree(row)
        self.connection_buttons.pop(identity, None)
        self.container.remove(expander)
        self.device_list.remove(row)

    def update_devices(self):
        if self._shutting_down:
            return
        for identity, (card, icon, button, status) in self.cards.items():
            device = self.window.workspace.devices[identity]
            card.set_visible(not (self.window.ui_settings.values['explorer_hide_disconnected']
                                 and device.type == 'ssh' and device.state == 'disconnected'))
            symbol = ("computer-symbolic" if device.type == "local" else
                      "network-transmit-receive-symbolic" if device.state == "connected" else
                      "network-idle-symbolic" if device.state == "connecting" else "network-offline-symbolic")
            icon.set_from_icon_name(symbol)
            row_icon = self.device_rows[identity].get_first_child()
            row_icon.set_from_icon_name(symbol)
            detail = workspace_header(device, device.navigation_state.current_path)[1]
            row_icon.get_next_sibling().set_label(device.display_name + '\n' + detail)
            row_icon.get_next_sibling().get_child().set_ellipsize(Pango.EllipsizeMode.END)
            row_icon.get_next_sibling().get_child().set_max_width_chars(22)
            row_icon.get_next_sibling().set_tooltip_text(device.display_name + '\n' + device.hostname + ' — ' + detail)
            status.set_text(device.state)
            label = root_label(device.navigation_state.current_path) if len(self.cards) == 1 and device.navigation_state.current_path else ('Local' if device.type == 'local' else 'SSH') + ' · ' + device.display_name
            button.set_label(label)
            button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
            button.get_child().set_max_width_chars(18)
            button.set_tooltip_text(device.display_name + '\n' + ("LOCAL" if device.type == "local" else "SSH " + device.hostname)
                                    + ' — ' + detail + '\nClick name to activate/reconnect; use the arrow to collapse or expand.')
            self.explorers[identity].identity.set_text(detail.split(' · ', 1)[1] if len(self.cards) > 1 else detail)
            self.explorers[identity].identity.set_tooltip_text(detail)
            self.explorers[identity].update_controls()
        self.arrange_cards()
        self.update_actions()

    def update_actions(self):
        if self._shutting_down or not self.explorers:
            return
        explorer = self.active_explorer
        valid = explorer.device.connected and bool(explorer.selected_parent()) and explorer.inline is None
        self.file_button.set_sensitive(valid)
        self.folder_button.set_sensitive(valid)
        self.refresh_button.set_sensitive(explorer.device.connected)

    def collapse_all(self):
        for explorer in self.explorers.values():
            if explorer.tree_model:
                for index in reversed(range(explorer.tree_model.get_n_items())):
                    row = explorer.tree_model.get_row(index)
                    if row: row.set_expanded(False)
        for card in self.cards.values(): card[0].set_expanded(False)

    def refresh_all(self):
        for explorer in self.explorers.values():
            if explorer.device.connected: explorer.refresh()

    def reset_sections(self):
        self.devices_section.set_expanded(True)
        self.files_section.set_expanded(True)
        self.hide_disconnected.set_active(self.window.ui_settings.values['explorer_hide_disconnected'])
        self.tree_height.set_value(self.window.ui_settings.values['explorer_tree_height'])
        for explorer in self.explorers.values(): explorer.apply_tree_size()
        for identity, card in self.cards.items():
            card[0].set_expanded(self.window.workspace.devices[identity].connected)

    def new_item(self, directory):
        explorer = self.active_explorer
        parent = explorer.selected_parent()
        if parent and explorer.device.connected:
            explorer.name_dialog("New Folder" if directory else "New File", parent,
                                 explorer.backend, explorer.session)

    def _entry_dialog(self, title, fields, accept):
        if self._shutting_down:
            return
        if self.window.dialog:
            self.window.dialog.present()
            return
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        area = dialog.get_content_area()
        area.set_spacing(8)
        for edge in ("top", "bottom", "start", "end"):
            getattr(area, "set_margin_" + edge)(12)
        entries = []
        for label, widget in fields:
            area.append(Gtk.Label(label=label, xalign=0, wrap=True))
            area.append(widget)
            entries.append(widget)
            if isinstance(widget, Gtk.Entry):
                widget.set_activates_default(True)
        errors = Gtk.Label(xalign=0, wrap=True)
        area.append(errors)
        self.window.dialog = dialog
        def response(dialog, answer):
            if self._shutting_down:
                return
            if answer == Gtk.ResponseType.ACCEPT:
                try:
                    accept(entries)
                except (ValueError, OSError) as error:
                    errors.set_text(str(error))
                    return
            self.window.dialog = None
            self.window.signals.disconnect(dialog)
            for widget in entries:
                self.window.signals.disconnect(widget)
            dialog.destroy()
        self.window.signals.connect(dialog, "response", response)
        dialog.present()

    def add_device_dialog(self):
        mode = Gtk.DropDown.new_from_strings(["SSH host", "Local folder"])
        target, name = Gtk.Entry(), Gtk.Entry()
        target.set_placeholder_text("SSH config alias, user@host, or absolute local folder")
        name.set_placeholder_text("Optional SSH display name")
        suggestions = ssh_config_hosts()
        if suggestions:
            # Selecting an alias is optional; arbitrary valid hosts still work.
            aliases = Gtk.DropDown.new_from_strings(["Choose SSH config alias…", *suggestions])
            self.window.signals.connect(aliases, "notify::selected",
                lambda choice, _: target.set_text(suggestions[choice.get_selected() - 1])
                if 0 < choice.get_selected() <= len(suggestions) else None)
        else:
            aliases = Gtk.Label(label="Authentication uses normal OpenSSH; passwords are never saved.", wrap=True)
        def accept(entries):
            if mode.get_selected() == 0:
                host = ssh_host(target.get_text().strip())
                self.window.add_ssh_device(host, name.get_text().strip() or None)
            else:
                path = navigation_path(target.get_text(), os.getcwd(), os.path.expanduser("~"))
                self.window.add_workspace_root("local", path)
                self.window.activate_device("local")
        self._entry_dialog("Add Device / Local Folder",
                           [("Type", mode), ("Location", target), ("Display name", name),
                            ("SSH config aliases", aliases)], accept)

    def root_dialog(self, identity):
        device = self.window.workspace.devices[identity]
        path = Gtk.Entry(text=device.navigation_state.current_path or "")
        def accept(entries):
            value = navigation_path(path.get_text(), device.navigation_state.current_path or "/",
                                    device.navigation_state.home)
            self.window.add_workspace_root(identity, value)
        self._entry_dialog("Pin Root — " + device.display_name,
                           [("Folder path (a workspace reference; no files are created)", path)], accept)

    def close(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.signals.close()
        for explorer in self.explorers.values():
            explorer.close()
        self.window = None

"""Native lazy filesystem tree. Expanding/refreshing NEVER sends terminal input."""
import datetime
import posixpath

from gi.repository import Gdk, Gio, GObject, Gtk, Pango

from ...backends import DisconnectedError
from ...filesystem import FileEntry
from ...sidebar_layout import root_label, indentation, breadcrumb, workspace_header
from ...navigation import child_path
from ...lifecycle import Signals


class TreeNode(GObject.Object):
    def __init__(self, entry, root=False, pinned=False, inline=None):
        super().__init__()
        self.entry, self.root = entry, root
        self.pinned, self.inline = pinned, inline
        self.children = None


class ExplorerSidebar(Gtk.Box):
    def __init__(self, window, device):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._shutting_down = False
        self.signals, self.factory_signals, self.popover_signals = Signals(), Signals(), Signals()
        self.window = window
        self.device = device
        self.inline, self.inline_entry, self.pending_select = None, None, None
        self.watches, self.changed_directories, self.bound_items, self.git_repositories = {}, set(), {}, {}
        self.watch_timer, self.change_timer = None, None
        self.set_size_request(0, -1)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(2)
        self.set_margin_bottom(2)
        self.backend, self.session, self.path, self.home = None, None, None, None
        self.connection_state, self.backend_generation = None, None
        self.generation = 0
        self.histories = {device.id: device.navigation_state.history}
        self.tree_model = None
        self.popover = None
        self.identity = Gtk.Label(xalign=0, selectable=True, ellipsize=Pango.EllipsizeMode.END, max_width_chars=24)
        self.identity.add_css_class("heading")
        self.identity.set_ellipsize(Pango.EllipsizeMode.END)
        self.identity.set_max_width_chars(16)
        self.append(self.identity)
        self.path_entry = Gtk.Entry()
        self.path_entry.set_width_chars(1)
        self.path_entry.set_max_width_chars(1)
        self.path_entry.set_hexpand(True)
        self.path_entry.set_placeholder_text("Directory path")
        self.signals.connect(self.path_entry, "activate", lambda entry: self.accept_path(entry.get_text()))
        self.path_stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.breadcrumb = Gtk.Button(has_frame=False, hexpand=True)
        self.breadcrumb.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START, width_chars=1, max_width_chars=12))
        self.signals.connect(self.breadcrumb, 'clicked', lambda *_: self.edit_path())
        self.path_stack.add_named(self.breadcrumb, 'breadcrumb')
        self.path_stack.add_named(self.path_entry, 'entry')
        self.path_stack.set_visible_child_name('breadcrumb')
        keys = Gtk.EventControllerKey()
        self.signals.connect(keys, 'key-pressed', self.path_key_pressed)
        self.path_entry.add_controller(keys)
        controls = Gtk.Box(spacing=4)
        self.back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.forward = Gtk.Button.new_from_icon_name("go-next-symbolic")
        self.parent = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        for button, callback in [(self.back, lambda *_: self.history_move(-1)),
                                 (self.forward, lambda *_: self.history_move(1)),
                                 (self.parent, lambda *_: self.navigate(posixpath.dirname(self.path) or "/")),
                                 (self.refresh_button, lambda *_: self.refresh())]:
            self.signals.connect(button, "clicked", callback)
            controls.append(button)
        for button, tooltip in ((self.back, 'Back'), (self.forward, 'Forward'), (self.parent, 'Parent directory'), (self.refresh_button, 'Refresh')):
            button.set_tooltip_text(tooltip)
            button.set_has_frame(False)
        self.append(controls)
        self.append(self.path_stack)
        self.run_ls = Gtk.CheckButton(label="ls after cd")
        self.run_ls.set_tooltip_text("Session-only preference; listings in this sidebar always use the filesystem backend.")
        # Session preference is exposed through Explorer overflow, not a permanent row.
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=16)
        self.signals.connect(self.status, "notify::label", lambda label, *_: label.set_tooltip_text(label.get_text()))
        self.append(self.status)
        factory = Gtk.SignalListItemFactory()
        self.factory_signals.connect(factory, "setup", self._setup_row)
        self.factory_signals.connect(factory, "bind", self._bind_row)
        self.factory_signals.connect(factory, "unbind", self._unbind_row)
        self.factory_signals.connect(factory, "teardown", self._teardown_row)
        self.list_view = Gtk.ListView(factory=factory)
        self.list_view.set_single_click_activate(False)
        self.signals.connect(self.list_view, "activate", self._activate)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll = scroll
        scroll.set_vexpand(False)
        scroll.set_min_content_height(0)
        scroll.set_max_content_height(window.ui_settings.values.get('explorer_tree_height', 240))
        scroll.set_propagate_natural_height(True)
        scroll.set_child(self.list_view)
        self.append(scroll)
        self.resize_handle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, height_request=12, focusable=True)
        self.resize_handle.set_cursor_from_name('ns-resize')
        self.resize_handle.set_tooltip_text('Drag to resize the folder list. Arrow keys adjust height; double-click or Home restores automatic height.')
        self.resize_handle.update_property([Gtk.AccessibleProperty.LABEL], ['Resize folder list'])
        self.resize_handle.append(Gtk.Separator(margin_top=5, margin_bottom=5))
        self.append(self.resize_handle)
        drag = Gtk.GestureDrag()
        self.signals.connect(drag, 'drag-begin', self.resize_begin)
        self.signals.connect(drag, 'drag-update', self.resize_update)
        self.signals.connect(drag, 'drag-end', lambda *_: self.window.save_ui())
        self.resize_handle.add_controller(drag)
        click = Gtk.GestureClick()
        self.signals.connect(click, 'pressed', lambda _, count, *args: self.reset_tree_size() if count == 2 else None)
        self.resize_handle.add_controller(click)
        keys = Gtk.EventControllerKey()
        self.signals.connect(keys, 'key-pressed', self.resize_key)
        self.resize_handle.add_controller(keys)
        self.apply_tree_size()
        self.update_controls()

    def apply_tree_size(self):
        values = self.window.ui_settings.values
        height = values.get('explorer_device_heights', {}).get(self.device.id)
        self.scroll.set_min_content_height(0)
        self.scroll.set_max_content_height(height or values['explorer_tree_height'])
        if height: self.scroll.set_min_content_height(height)

    def set_tree_size(self, height):
        values = self.window.ui_settings.values
        sizes = {key: value for key, value in values.get('explorer_device_heights', {}).items()
                 if key in self.window.workspace.devices}
        sizes[self.device.id] = max(40, min(1200, round(height)))
        values['explorer_device_heights'] = sizes
        self.apply_tree_size()

    def reset_tree_size(self):
        self.window.ui_settings.values.get('explorer_device_heights', {}).pop(self.device.id, None)
        self.apply_tree_size()
        self.window.save_ui()

    def resize_begin(self, *_):
        self.resize_origin = self.scroll.get_height()
        self.resize_handle.grab_focus()

    def resize_update(self, gesture, dx, dy):
        self.set_tree_size(self.resize_origin + dy)

    def resize_key(self, controller, key, code, modifiers):
        if key == Gdk.KEY_Home:
            self.reset_tree_size(); return True
        if key not in (Gdk.KEY_Up, Gdk.KEY_Down): return False
        self.set_tree_size(self.scroll.get_height() + (-20 if key == Gdk.KEY_Up else 20))
        self.window.save_ui()
        return True

    def edit_path(self):
        if self._shutting_down: return
        self.path_stack.set_visible_child_name('entry')
        self.path_entry.set_text(self.path or '')
        self.path_entry.grab_focus(); self.path_entry.select_region(0, -1)

    def accept_path(self, text):
        if self.navigate(text): self.path_stack.set_visible_child_name('breadcrumb')

    def path_key_pressed(self, controller, key, keycode, modifiers):
        if key == Gdk.KEY_Escape:
            self.path_entry.set_text(self.path or '')
            self.path_stack.set_visible_child_name('breadcrumb')
            self.breadcrumb.grab_focus()
            return True
        return False

    def apply_sidebar_scale(self, scale):
        for item in self.bound_items:
            widget = item.get_child()
            widget.icon.set_pixel_size(round(16 * scale))
            row = item.get_item()
            if row and hasattr(widget, 'set_indent_for_depth'):
                widget.set_margin_start(round(indentation(row.get_depth()) * scale))

    @property
    def history(self):
        return self.device.history_for_path(self.path)

    def set_context(self, backend, session, path=None, home=None, state=None):
        if self._shutting_down:
            return
        generation = getattr(backend, "generation", 0)
        changed = (backend is not self.backend or session != self.session or path != self.path
                   or state != self.connection_state or generation != self.backend_generation)
        self.backend, self.session, self.path, self.home = backend, session, path, home
        self.connection_state, self.backend_generation = state, generation
        _, detail = workspace_header(self.device, path)
        workspace = getattr(self.window, 'workspace_sidebar', None)
        self.identity.set_text(detail.split(' · ', 1)[1] if workspace and len(workspace.cards) > 1 else detail)
        self.identity.set_tooltip_text(self.device.display_name + '\n' + (path or ''))
        self.path_entry.set_text(path or '')
        self.path_entry.set_tooltip_text(path or 'Directory path')
        self.breadcrumb.get_child().set_text(breadcrumb(path))
        self.breadcrumb.set_tooltip_text(path or 'Directory path')
        self.path_stack.set_visible_child_name('breadcrumb')
        if changed and (self.device.connected or self.tree_model is None):
            self.refresh()
        elif not self.device.connected:
            self.status.set_text("Disconnected. Click the device name to reconnect.")
        self.update_controls()

    @staticmethod
    def _remote_identity(backend):
        connection = backend.connection
        if connection.username and "@" not in connection.host:
            return connection.username + "@" + connection.host
        return connection.host

    def update_controls(self):
        if self._shutting_down:
            return
        connected = self.backend is not None and self.device.connected
        terminal = self.window.device_terminal(self.device.id)
        ready = (connected and bool(self.path) and terminal is not None and terminal.at_prompt
                 and not terminal.navigation_pending)
        self.path_entry.set_sensitive(ready)
        self.breadcrumb.set_sensitive(ready)
        self.back.set_sensitive(ready and self.history.index > 0)
        self.forward.set_sensitive(ready and self.history.index + 1 < len(self.history.paths))
        self.parent.set_sensitive(ready and self.path != "/")
        self.refresh_button.set_sensitive(connected and bool(self.path) and self.inline is None)

    def navigate(self, text, expected_session=None, check_session=False):
        if self._shutting_down:
            return False
        if check_session and expected_session != self.session:
            self.status.set_text("The active filesystem changed; reopen the context menu.")
            return False
        self.window.activate_device(self.device.id)
        if self.device.type == 'ssh' and self.session is None:
            self.status.set_text('Wait for this device’s SSH shell before navigating its directory.')
            return False
        return self.window.navigate_terminal(text, self.session, self.run_ls.get_active())

    def history_move(self, step):
        if self._shutting_down:
            return
        self.window.activate_device(self.device.id)
        target = self.history.target(step)
        if target and not self.navigate(target):
            self.history.cancel()

    def refresh(self):
        if self._shutting_down:
            return
        self.histories = {self.device.id: self.device.navigation_state.history,
                          **{root.path: root.navigation.history for root in self.device.roots}}
        self._remember_expansion()
        if self.inline:
            self.device.navigation_state.expanded.add(self.inline["parent"])
        self.generation += 1
        self._close_popover()
        if self.backend is None:
            self.list_view.set_model(None)
            self.status.set_text("Waiting for the shell directory…")
            return
        root = Gio.ListStore.new(TreeNode)
        paths = [self.path] if self.path else []
        paths += [item.path for item in self.device.roots if item.path not in paths]
        if self.inline and self.inline["parent"] not in paths:
            paths.append(self.inline["parent"])
        pinned = {item.path for item in self.device.roots}
        for path in paths:
            root.append(TreeNode(FileEntry(root_label(path), path, "d", 0, 0, 0, "", ""),
                                 root=True, pinned=path in pinned))
        self.tree_model = Gtk.TreeListModel.new(root, False, False, self._children)
        selection = Gtk.SingleSelection.new(self.tree_model)
        self.signals.disconnect(getattr(self, "selection", None))
        self.selection = selection
        self.signals.connect(selection, "notify::selected", lambda *_: self._selection_changed())
        self.list_view.set_model(selection)
        if self.tree_model.get_n_items():
            self.tree_model.get_row(0).set_expanded(True)
        self._restore_state()
        if not self.device.connected:
            self.status.set_text("Disconnected. Click the device name to reconnect; workspace roots remain available.")
        self.schedule_watches()

    def schedule_watches(self):
        if self._shutting_down or not hasattr(self.window, 'file_watch'): return
        self.window.sources.remove(self.watch_timer)
        self.watch_timer = self.window.sources.timeout(100, self.sync_watches)

    def sync_watches(self):
        self.watch_timer = None
        if self._shutting_down: return
        paths = set()
        if self.tree_model is not None and self.device.connected:
            for index in range(self.tree_model.get_n_items()):
                row = self.tree_model.get_row(index)
                node = row.get_item()
                if node.entry.kind == 'd' and (node.root or row.get_expanded()): paths.add(node.entry.path)
        for path in set(self.watches) - paths: self.watches.pop(path).close()
        api = self.window.plugins.features['explorer_files']
        for path in paths - set(self.watches):
            try: self.watches[path] = api.watch(device_id=self.device.id, path=path, callback=self.file_changed)
            except (ValueError, OSError) as error: self.status.set_text('Watch: ' + str(error))

    def file_changed(self, event):
        if self._shutting_down: return
        if event.event == 'moved' and event.old_path == self.selected_path(): self.pending_select = event.path
        self.changed_directories.add(posixpath.dirname(event.path))
        if event.old_path: self.changed_directories.add(posixpath.dirname(event.old_path))
        self.window.sources.remove(self.change_timer)
        self.change_timer = self.window.sources.timeout(350, self.refresh_changed)

    def refresh_changed(self):
        self.change_timer = None
        if self._shutting_down or self.inline or not self.device.connected: return
        paths, self.changed_directories = self.changed_directories, set()
        if self.tree_model is None: return
        nodes = [self.tree_model.get_row(index).get_item() for index in range(self.tree_model.get_n_items())]
        for node in nodes:
            if node.entry.path in paths and node.children is not None:
                self.reload_directory(node)

    def reload_directory(self, node):
        backend, generation, path, children = self.backend, self.generation, node.entry.path, node.children
        selected = self.pending_select or self.selected_path()
        def loaded(entries, error):
            if self._shutting_down or generation != self.generation or backend is not self.backend: return
            if error:
                self.status.set_text('Live refresh: ' + str(error))
                return
            old = {children.get_item(index).entry.path: children.get_item(index) for index in range(children.get_n_items())}
            updated = []
            for entry in entries:
                item = old.get(entry.path)
                if item is None or item.entry.kind != entry.kind: item = TreeNode(entry)
                else: item.entry = entry
                updated.append(item)
            self._remember_expansion()
            expanded = set(self.device.navigation_state.expanded)
            children.splice(0, children.get_n_items(), updated)
            self.device.navigation_state.expanded.update(expanded)
            self.pending_select = selected
            self._restore_state()
            self.schedule_watches()
        self.window.work(lambda: backend.list_directory(path), loaded)

    def set_git_status(self, root, branch, statuses):
        if self._shutting_down: return
        decorated = dict(statuses)
        for path in statuses:
            parent = posixpath.dirname(path)
            while parent == root or parent.startswith(root.rstrip('/') + '/'):
                decorated.setdefault(parent, 'M')
                if parent == root: break
                parent = posixpath.dirname(parent)
        self.git_repositories[root] = branch, decorated
        for item, node in list(self.bound_items.items()):
            self.decorate_row(item.get_child(), node)

    def decorate_row(self, widget, node):
        path, status, suffix = node.entry.path, '', ''
        for root, (branch, statuses) in self.git_repositories.items():
            if path == root: suffix = ' — ' + branch
            if path in statuses: status = statuses[path]
        widget.name_label.set_text(node.entry.name)
        if hasattr(widget, 'git_label'):
            widget.git_label.set_text(status)
            widget.git_label.set_visible(bool(status) and node.inline is None)
            widget.git_label.set_tooltip_text(status + suffix)
        if hasattr(widget.name_label, 'set_tooltip_text'): widget.name_label.set_tooltip_text(path + suffix)

    def selected_path(self):
        if self.tree_model is not None and hasattr(self, 'selection'):
            row = self.selection.get_selected_item()
            if row and not row.get_item().inline:
                return row.get_item().entry.path
        return self.path

    def selected_parent(self):
        if self.tree_model is not None and hasattr(self, "selection"):
            row = self.selection.get_selected_item()
            if row and not row.get_item().inline:
                entry = row.get_item().entry
                return entry.path if entry.is_directory else posixpath.dirname(entry.path)
        return self.path or (self.device.roots[0].path if self.device.roots else None)

    def _selection_changed(self):
        if self._shutting_down or not hasattr(self.window, "workspace_sidebar"):
            return
        # Selection preserves every other device's tree; it does not cd.
        self.window.workspace_sidebar.update_actions()
        if hasattr(self.window, 'plugins'): self.window.plugins.emit('workspace')

    def _remember_expansion(self):
        if self.tree_model is None:
            return
        expanded = self.device.navigation_state.expanded
        for index in range(self.tree_model.get_n_items()):
            row = self.tree_model.get_row(index)
            path = row.get_item().entry.path
            if row.get_expanded():
                expanded.add(path)
            else:
                expanded.discard(path)
        for root in self.device.roots:
            root.navigation.expanded = {path for path in expanded if path == root.path or path.startswith(root.path.rstrip("/") + "/")}

    def _restore_state(self):
        if self.tree_model is None:
            return
        index = 0
        while index < self.tree_model.get_n_items():
            row = self.tree_model.get_row(index)
            node = row.get_item()
            if node.entry.path in self.device.navigation_state.expanded and node.entry.is_directory and not node.inline:
                row.set_expanded(True)
            if self.pending_select == node.entry.path and not node.inline:
                self.selection.set_selected(index)
                self.pending_select = None
            index += 1

    def _children(self, node):
        if self._shutting_down or not node.entry.is_directory or node.inline:
            return None
        if node.children is not None:
            return node.children
        children = node.children = Gio.ListStore.new(TreeNode)
        if not self.device.connected:
            return children
        backend, generation, path = self.backend, self.generation, node.entry.path
        self.status.set_text("Loading " + node.entry.path + "…")

        def loaded(entries, error):
            if self._shutting_down or generation != self.generation or backend is not self.backend:
                return  # Old SSH/local results must never populate the new tree.
            if error:
                self.error(error, backend)
            else:
                nodes = [TreeNode(entry, inline=self.inline if self.inline and self.inline.get("entry")
                                  and self.inline["entry"].path == entry.path else None) for entry in entries]
                if self.inline and self.inline["parent"] == path and self.inline["entry"] is None:
                    nodes.append(self._inline_node())
                children.splice(0, children.get_n_items(), nodes)
                self.status.set_text(f"{len(entries)} entries — {path}")
                self._restore_state()
                self.schedule_watches()
        # Capture a plain path, not a native TreeNode, in the worker job.
        self.window.work(lambda: backend.list_directory(path), loaded)
        return children

    def _setup_row(self, factory, item):
        if self._shutting_down:
            return
        item.set_focusable(False)
        expander = Gtk.TreeExpander()
        if hasattr(expander, "set_indent_for_depth"): expander.set_indent_for_depth(False)
        content = Gtk.Box(spacing=6)
        icon = Gtk.Image()
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        label.set_width_chars(1)
        label.set_max_width_chars(12)
        content.append(icon)
        content.append(label)
        entry = Gtk.Entry()
        entry.set_width_chars(1)
        entry.set_max_width_chars(1)
        entry.set_hexpand(True)
        entry.set_placeholder_text("Enter name; Enter confirms, Escape cancels")
        content.append(entry)
        git_label = Gtk.Label(width_chars=1)
        git_label.add_css_class('dim-label')
        content.append(git_label)
        expander.git_label = git_label
        expander.set_child(content)
        expander.icon, expander.name_label, expander.inline_entry = icon, label, entry
        item.set_child(expander)
        gesture = Gtk.GestureClick()
        gesture.set_button(3)

        def pressed(gesture, count, x, y):
            if self._shutting_down:
                return
            row = item.get_item()
            if row:
                self._context_menu(expander, row.get_item(), x, y)
        item._naui_signals = Signals()
        item._naui_signals.connect(entry, "activate", lambda widget: self.commit_inline(widget.get_text()))
        def changed(widget):
            if self._shutting_down or self.inline is None:
                return
            row = item.get_item()
            if row and row.get_item().inline is self.inline:
                self.inline["name"] = widget.get_text()
        item._naui_signals.connect(entry, "changed", changed)
        keys = Gtk.EventControllerKey()
        def key_pressed(controller, key, keycode, modifiers):
            if key == Gdk.KEY_Escape and not self._shutting_down:
                self.cancel_inline()
                return True
            return False
        item._naui_signals.connect(keys, "key-pressed", key_pressed)
        entry.add_controller(keys)
        item._naui_signals.connect(gesture, "pressed", pressed)
        expander.add_controller(gesture)
        # Explicitly handle double-clicks on the name/icon content. The native
        # disclosure arrow is outside this box and retains expansion-only behavior.
        activate = Gtk.GestureClick()
        activate.set_button(1)
        activate.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def double_clicked(gesture, count, x, y):
            if self._shutting_down:
                return
            if count == 1:
                self.window.activate_device(self.device.id, reconnect=False)
            if count == 1:
                row = item.get_item()
                if row and not row.get_item().inline and not row.get_item().entry.is_directory:
                    entry_to_open = row.get_item().entry
                    self.window.sources.idle(lambda: self.open_entry(entry_to_open, preview=True) if not self._shutting_down else None)
            if count == 2:
                row = item.get_item()
                if row and not row.get_item().inline:
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                    self.open_entry(row.get_item().entry, preview=False)
        item._naui_signals.connect(activate, "pressed", double_clicked)
        content.add_controller(activate)
        if hasattr(self.window, "transfer_panel"):
            self.window.transfer_panel.attach_row(content, item, self, item._naui_signals)

    def _bind_row(self, factory, item):
        if self._shutting_down:
            return
        row = item.get_item()
        node = row.get_item()
        expander = item.get_child()
        expander.set_list_row(row)
        if hasattr(expander, "set_indent_for_depth"): expander.set_margin_start(round(indentation(row.get_depth()) * self.window.ui_settings.values['sidebar_scale'] / 100))
        expander.icon.set_pixel_size(round(16 * self.window.ui_settings.values['sidebar_scale'] / 100))
        expander.icon.set_from_icon_name("folder-symbolic" if node.entry.is_directory else "text-x-generic-symbolic")
        self.bound_items[item] = node
        self.decorate_row(expander, node)
        item._naui_signals.connect(row, 'notify::expanded', lambda *_: self.schedule_watches())
        expander.name_label.set_visible(node.inline is None)
        expander.inline_entry.set_visible(node.inline is not None)
        if node.inline:
            expander.inline_entry.set_text(node.inline["name"])
            expander.inline_entry.set_sensitive(not node.inline["busy"])
            self.inline_entry = expander.inline_entry
            self.window.sources.idle(self._focus_inline, item, node)
        expander.set_tooltip_text(node.entry.path + (" (symlink)" if node.entry.is_symlink else ""))

    def _focus_inline(self, item, node):
        if self._shutting_down or self.inline is not node.inline:
            return
        row = item.get_item()
        if row and row.get_item() is node:
            item.get_child().inline_entry.grab_focus()

    def _unbind_row(self, factory, item):
        self.bound_items.pop(item, None)
        child = item.get_child()
        if child is not None:
            if hasattr(item, '_naui_signals'): item._naui_signals.disconnect(child.get_list_row())
            child.set_list_row(None)

    def _teardown_row(self, factory, item):
        self.bound_items.pop(item, None)
        if hasattr(item, "_naui_signals"):
            item._naui_signals.close()
        item.set_child(None)

    def _activate(self, view, position):
        if self._shutting_down or self.tree_model is None:
            return
        row = self.tree_model.get_item(position)
        if row and not row.get_item().inline:
            self.open_entry(row.get_item().entry)

    def open_entry(self, entry, backend=None, session=None, *, preview=False):
        if self._shutting_down:
            return
        backend = self.backend if backend is None else backend
        session = self.session if backend is self.backend else session
        if entry.is_directory:
            self.navigate(entry.path, session, check_session=True)
        elif entry.kind in {"f", "lf"}:
            if self.device.connected:
                self.window.open_path(backend, entry.path, session, preview=preview)
            else:
                self.status.set_text("Reconnect this device before opening a file.")
        else:
            self.status.set_text("Only regular files can be opened.")

    def _context_menu(self, widget, node, x, y):
        if self._shutting_down:
            return
        if node.inline:
            return
        self.window.activate_device(self.device.id, reconnect=False)
        self._close_popover()
        popover = self.popover = Gtk.Popover()
        popover.set_parent(widget)
        rectangle = Gdk.Rectangle()
        rectangle.x, rectangle.y, rectangle.width, rectangle.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rectangle)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        popover.set_child(box)
        backend, session, entry = self.backend, self.session, node.entry
        parent = entry.path if entry.is_directory else posixpath.dirname(entry.path)

        def button(label, callback, sensitive=True):
            control = Gtk.Button(label=label)
            control.set_has_frame(False)
            control.set_sensitive(sensitive)
            self.popover_signals.connect(control, "clicked", lambda *_: None if self._shutting_down else (popover.popdown(), callback()))
            box.append(control)
        button("Open", lambda: self.open_entry(entry, backend, session), entry.is_directory or self.device.connected)
        button("Open in Editor", lambda: self.window.open_file(backend, entry.path, session),
               entry.kind in {"f", "lf"} and self.device.connected)
        if hasattr(self.window, 'plugins'):
            from ...transfers import FileLocation
            location = FileLocation(self.device.id, entry.path)
            for label, predicate, action in self.window.plugins.file_actions.values():
                if predicate(entry):
                    button(label, lambda action=action: action(location, entry), self.device.connected)
        button("Open Terminal Here", lambda: self.navigate(parent, session, check_session=True))
        button("Copy Path", lambda: self.get_clipboard().set(entry.path))
        writable = self.device.connected and self.inline is None
        button("New File", lambda: self.name_dialog("New File", parent, backend, session), writable)
        button("New Folder", lambda: self.name_dialog("New Folder", parent, backend, session), writable)
        button("Rename", lambda: self.name_dialog("Rename", parent, backend, session, entry), writable and not node.root)
        button("Delete", lambda: self.confirm_delete(entry, backend, session), writable and not node.root)
        if node.pinned:
            button("Remove from Workspace", lambda: self.window.remove_workspace_root(self.device.id, entry.path))
        else:
            button("Pin as Workspace Root", lambda: self.window.add_workspace_root(self.device.id, entry.path), entry.is_directory)
        button("Refresh", self.refresh, self.device.connected and self.inline is None)
        button("Properties", lambda: self.show_properties(entry.path, backend), self.device.connected)
        popover.popup()

    def _after_operation(self, backend, error):
        if self._shutting_down:
            return
        if error:
            self.error(error, backend)
        elif backend is self.backend:
            self.refresh()
            self.status.set_text("Filesystem operation completed")
        else:
            self.window.set_status("Filesystem operation completed on " + getattr(getattr(backend, "connection", None), "host", "LOCAL"))

    def error(self, error, backend=None):
        if self._shutting_down:
            return
        self.status.set_text(str(error))
        if isinstance(error, DisconnectedError):
            backend = self.backend if backend is None else backend
            if not backend.connected:
                self.window.disconnect_session(self.device.session_id, str(error))

    def name_dialog(self, action, parent, backend, session, entry=None):
        """Start an inline transaction. No filesystem work happens before Enter."""
        if self._shutting_down:
            return
        if self.inline is not None:
            self.status.set_text("Confirm or cancel the current name first.")
            return
        if not self.device.connected:
            self.status.set_text("Reconnect this device before changing files.")
            return
        self.inline = {"action": action, "parent": parent, "backend": backend,
                       "entry": entry, "name": entry.name if entry else "", "busy": False}
        self.device.navigation_state.expanded.add(parent)
        self.refresh()
        self.window.workspace_sidebar.update_actions()

    def _inline_node(self):
        operation = self.inline
        entry = FileEntry("", operation["parent"], "d" if operation["action"] == "New Folder" else "f",
                          0, 0, 0, "", "")
        return TreeNode(entry, inline=operation)

    def commit_inline(self, name):
        if self._shutting_down or self.inline is None or self.inline["busy"]:
            return False
        operation = self.inline
        backend, entry = operation["backend"], operation["entry"]
        try:
            destination = child_path(operation["parent"], name)
            self.window.check_mutation(backend, entry.path if entry else destination, rename=bool(entry))
        except ValueError as error:
            self.status.set_text(str(error))
            return False
        operation["name"], operation["busy"] = name, True
        if self.inline_entry:
            self.inline_entry.set_sensitive(False)
        def completed(path, error):
            if self._shutting_down or self.inline is not operation:
                return
            if error:
                operation["busy"] = False
                if self.inline_entry:
                    self.inline_entry.set_sensitive(True)
                self.error(error, backend)
            else:
                self.inline = self.inline_entry = None
                self.pending_select = path
                self.device.navigation_state.expanded.add(operation["parent"])
                self.refresh()
                self.status.set_text("Created/renamed " + self.device.display_name + ":" + path)
            self.window.workspace_sidebar.update_actions()
        # Plain FileEntry/backend/paths only; no TreeNode or GTK in the worker.
        action = operation["action"]
        def job():
            if entry:
                backend.rename(entry.path, destination, entry.revision)
            else:
                backend.create(destination, directory=action == "New Folder")
            return destination
        self.window.work(job, completed)
        return True

    def cancel_inline(self):
        if self._shutting_down or self.inline is None:
            return
        if self.inline["busy"]:
            self.status.set_text("Wait for the confirmed filesystem operation to finish.")
            return
        self.inline = self.inline_entry = None
        self.refresh()
        self.window.workspace_sidebar.update_actions()

    def confirm_delete(self, entry, backend, session):
        if self._shutting_down:
            return
        try:
            self.window.check_mutation(backend, entry.path)
        except ValueError as error:
            self.error(error)
            return
        identity = self.device.display_name
        self.window.confirm_action("Delete permanently — " + identity,
                                   "Delete " + repr(entry.name) + " from " + identity + ":" + entry.path
                                   + "?\nOnly files, symlinks and empty directories can be deleted.",
                                   "Delete", lambda: self._confirmed_delete(entry, backend))

    def _confirmed_delete(self, entry, backend):
        if self._shutting_down:
            return
        try:
            self.window.check_mutation(backend, entry.path)
        except ValueError as error:
            self.error(error, backend)
            return
        self.window.work(lambda: backend.delete(entry.path, entry.revision),
                         lambda result, error: self._after_operation(backend, error))

    def show_properties(self, path, backend):
        if self._shutting_down:
            return
        def loaded(entry, error):
            if self._shutting_down:
                return
            if error:
                self.error(error, backend)
                return
            identity = "LOCAL" if not hasattr(backend, "connection") else self._remote_identity(backend)
            try:
                modified = datetime.datetime.fromtimestamp(entry.mtime).astimezone().isoformat()
            except (ValueError, OverflowError, OSError):
                modified = str(entry.mtime) + " (Unix timestamp)"
            text = (f"{identity}\n{path}\nType: {entry.kind}\nSize: {entry.size} bytes\n"
                    f"Permissions: {entry.mode:04o}\nOwner UID:GID: {entry.owner}\nModified: {modified}")
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                       destroy_with_parent=True,
                                       text="Properties", secondary_text=text,
                                       buttons=Gtk.ButtonsType.CLOSE)
            def response(dialog, *_):
                self.window.signals.disconnect(dialog)
                dialog.destroy()
            self.window.signals.connect(dialog, "response", response)
            dialog.present()
        self.window.work(lambda: backend.properties(path), loaded)

    def close(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.window.sources.remove(self.watch_timer)
        self.window.sources.remove(self.change_timer)
        for watch in self.watches.values(): watch.close()
        self.watches.clear()
        self.bound_items.clear()
        self.generation += 1
        self.signals.close()
        self._close_popover()
        # Unbind/teardown callbacks must run while the row children are alive.
        # Detach native models before disconnecting the factory callbacks.
        self.list_view.set_model(None)
        self.list_view.set_factory(None)
        self.factory_signals.close()
        self.tree_model = None
        self.selection = None
        self.inline = self.inline_entry = None
        self.window = None

    def _close_popover(self):
        self.popover_signals.close()
        if self.popover:
            self.popover.popdown()
            self.popover.unparent()
            self.popover = None

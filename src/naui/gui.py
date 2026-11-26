"""GTK 4 notebook UI. File I/O runs off the UI thread; edits stay in memory."""
import os
from pathlib import Path
import sys
import uuid

from gi.repository import Gdk, Gtk, GtkSource, Vte, Pango

from .backends import DisconnectedError
from .paths import resolve_path
from .protocol import verify_signature
from .ssh import Connection
from .terminal import TerminalPane
from .navigation import PREPARE_NAVIGATION, PREPARE_RUN, cd_command, navigation_path
from .lifecycle import FileWork, Signals, Sources
from .preview import PreviewCache
from .viewer_registry import ViewerRegistry
from .viewers import AudioViewer, ImageViewer, UnsupportedViewer, VideoViewer
from .metadata import DESCRIPTION, NAME, __version__, project_url
from .workspace import Workspace, WorkspaceStore, TerminalState, connection_metadata, ssh_host
from .workspace_sidebar import WorkspaceSidebar
from .plugin_manager import PluginManager
from .authentication import AuthenticationManager
from .transfers import TransferManager
from .transfer_ui import TransferPanel
from .session_lifecycle import classify_exit
from .file_watch import FileWatchService
from .commands import CommandService
from .external_changes import ExternalChange
from .plugin_views import ReadOnlyView, SidebarSection, BrowserView
from .ui_settings import UISettings
from .editor_model import EditorDocument, save_text
from .editor_view import EditorController, GtkBufferAdapter, show_editor_settings
from .transfers import FileLocation


class EditorPane(Gtk.Box):
    # Existing filesystem/LSP callers keep their interface. Metadata ownership
    # lives in a toolkit-independent document rather than in focus/global state.
    path = property(lambda self: self.document.path, lambda self, value: setattr(self.document, 'path', value))
    backend = property(lambda self: self.document.backend, lambda self, value: setattr(self.document, 'backend', value))
    device_id = property(lambda self: self.document.device_id, lambda self, value: setattr(self.document, 'device_id', value))
    revision = property(lambda self: self.document.revision, lambda self, value: setattr(self.document, 'revision', value))
    external = property(lambda self: self.document.external)

    def __init__(self, window, backend, path, session=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._shutting_down = False
        self.signals = Signals()
        self.copy_dialog = None
        self.window, self.session = window, session
        device = window.workspace.device_for_backend(backend)
        self.document = EditorDocument(device.id if device else None, path, backend)
        self.loading, self.saving, self.disconnected = True, False, False
        self.watch_handle, self.watch_timer = None, None
        self.watch_busy, self.watch_again = False, False
        self.label = Gtk.Label()
        self.buffer = GtkSource.Buffer()
        self.document.buffer = GtkBufferAdapter(self.buffer)
        self.buffer.set_enable_undo(True)
        self.buffer.set_highlight_syntax(True)
        schemes = GtkSource.StyleSchemeManager.get_default()
        for scheme_id in ("Adwaita-dark", "classic-dark", "oblivion"):
            scheme = schemes.get_scheme(scheme_id)
            if scheme is not None:
                self.buffer.set_style_scheme(scheme)
                break
        self.signals.connect(self.buffer, "modified-changed", lambda *_: self.update_label())
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.add_css_class('naui-code')
        self.view.set_monospace(True)
        self.view.set_show_line_numbers(True)
        self.view.set_highlight_current_line(True)
        self.view.set_auto_indent(True)
        self.view.set_tab_width(4)
        self.view.set_editable(False)
        self.view.set_hexpand(True)
        self.view.set_vexpand(True)
        self.editing = EditorController(self)
        self.status = Gtk.Label(label="Loading…", xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(80)
        self.status.set_selectable(True)
        self.status.set_margin_start(8)
        self.status.set_margin_end(8)
        self.append(self.status)
        self.external_controls = Gtk.Box(spacing=4)
        for title, action in [('Reload External Version', self.reload_external), ('Compare', self.compare_external),
                              ('Keep My Version', self.keep_external), ('Save As…', lambda: window.save_copy(self)),
                              ('Close', lambda: window.close_editor(self))]:
            control = Gtk.Button(label=title)
            self.signals.connect(control, 'clicked', lambda *_, action=action: action())
            self.external_controls.append(control)
            if title == 'Keep My Version': self.keep_button = control
        self.external_controls.set_visible(False)
        self.append(self.external_controls)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.view)
        scroll.set_vexpand(True)
        self.append(scroll)
        if hasattr(window, "code_navigation"):
            window.code_navigation.build_editor(self)
        self.update_label()

    @property
    def dirty(self):
        return self.document.dirty

    def text(self):
        return self.document.text()

    def focus_in_view(self):
        focus = self.window.get_focus()
        return focus is self.view or bool(focus and focus.is_ancestor(self.view))

    def command(self, name):
        return self.editing.execute(name)

    def update_label(self):
        if self._shutting_down:
            return
        title = Path(self.path).name or self.path
        device = self.window.workspace.devices.get(self.device_id)
        owner = device.display_name if device and device.type == "ssh" else "LOCAL"
        if device is None and hasattr(self.backend, "connection"):
            owner = self.backend.connection.host
        title += " — " + owner
        if self.dirty:
            if not self.loading: self.window.notebook.keep_open(self)
            title = "● " + title
        if self.disconnected:
            title += " [disconnected]"
        elif self.external.snapshot is not None:
            title += ' [deleted]' if self.external.deleted else ' [external change]'
        elif self.loading:
            title += " [loading]"
        self.label.set_text(title)
        self.label.set_tooltip_text(self.path)

    def loaded(self, snapshot):
        if self._shutting_down or self.window._shutting_down:
            return
        self.document.loaded(snapshot)
        self.editing.configure()
        self.external_controls.set_visible(False)
        self.buffer.begin_irreversible_action()
        self.buffer.set_text(snapshot.content)
        self.buffer.end_irreversible_action()
        self.buffer.set_modified(False)
        self.loading = False
        from .session_restore import SessionRestore
        SessionRestore.place(self)
        self.editing.loaded(snapshot.content)
        self.view.set_editable(True)
        self.status.set_text(self.path + (" — new file" if snapshot.revision == "missing" else ""))
        self.update_label()
        if hasattr(self.window, "code_navigation"):
            self.window.code_navigation.loaded(self)
        if hasattr(self.window, 'file_watch'):
            self.watch_file()
            self.window.sources.remove(self.watch_timer)
            self.watch_timer = self.window.sources.timeout(300, self.check_external)
        if self.window.current_editor() is self:
            self.view.grab_focus()

    def failed(self, error):
        if self._shutting_down or self.window._shutting_down:
            return
        if isinstance(error, DisconnectedError) and not self.backend.connected:
            self.status.set_text(str(error))
            self.disconnected = True
            self.window.disconnect_session(self.session, str(error))
        else:
            self.status.set_text(str(error))
        if self.loading:
            self.loading = False
            self.update_label()

    def mark_disconnected(self, reason="SSH session ended; editor contents preserved. Save a local copy to keep your work."):
        if self._shutting_down or self.window._shutting_down:
            return
        self.disconnected = True
        self.status.set_text(reason)
        self.update_label()

    def save(self):
        if self._shutting_down or self.window._shutting_down or self.loading or self.saving:
            return
        if self.revision is None:
            self.status.set_text("File could not be loaded. Save a copy to a new local file.")
            return
        if self.disconnected:
            self.status.set_text("Disconnected. Your edits are preserved; click the device to reconnect, or use Save a copy.")
            return
        original = self.text()
        preferences = self.window.ui_settings.values
        content = save_text(original, trim=preferences['editor_trim'], final_newline=preferences['editor_final_newline'])
        if content != original:
            # Save transforms are explicit preferences and one undoable edit.
            from .editor_commands import Edit
            cursor = min(self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset(),len(content))
            anchor = min(self.buffer.get_iter_at_mark(self.buffer.get_selection_bound()).get_offset(),len(content))
            self.editing.apply(Edit(0,len(original),content,anchor,cursor))
        generation = getattr(self.backend, "generation", 0)
        self.saving = True
        self.status.set_text("Saving…")

        def saved(snapshot, error):
            if self._shutting_down or self.window._shutting_down:
                return
            self.saving = False
            if path != self.path:
                self.buffer.set_modified(True)
                self.status.set_text('File renamed during save; contents preserved. Check the external version before saving again.')
                return
            if generation != getattr(self.backend, "generation", 0):
                self.buffer.set_modified(True)
                self.status.set_text("Connection changed during save; result unconfirmed. Contents preserved. Retry and resolve any revision conflict.")
                self.update_label()
                return
            if error:
                self.failed(error)
                return
            self.revision = snapshot.revision
            if hasattr(self, 'external'):
                self.external.clear()
                self.external_controls.set_visible(False)
            # Editing remains usable while SSH writes. Later edits remain dirty.
            self.buffer.set_modified(self.text() != content)
            self.status.set_text(self.path + " — saved")
            self.update_label()
            if hasattr(self.window, "code_navigation"):
                self.window.code_navigation.saved(self, content)
            if hasattr(self.window, 'plugins'):
                self.window.plugins.emit('workspace')

        # A worker must not retain this Gtk.Box or its native Buffer/View.
        backend, path, revision = self.backend, self.path, self.revision
        self.window.work(lambda: backend.write(path, content, revision), saved)

    def watch_file(self):
        if self.watch_handle: self.watch_handle.close()
        import posixpath
        try: self.watch_handle = self.window.file_watch.watch(FileLocation(self.device_id, posixpath.dirname(self.path)), self.file_changed)
        except (ValueError, OSError) as error: self.status.set_text('Live file watching unavailable: ' + str(error))

    def file_changed(self, event):
        if self._shutting_down or self.window._shutting_down: return
        if event.event == 'moved' and event.old_path and self.path == event.old_path:
            old = self.path
            self.path = event.path
            self.editing.loaded(self.text())
            self.window.code_navigation.relocated(self, old)
            self.watch_file()
            self.update_label()
            self.status.set_text('File renamed externally: ' + old + ' → ' + self.path)
        elif event.path != self.path:
            return
        if event.event == 'deleted':
            from .backends import FileSnapshot
            self.external.observe(self.revision, FileSnapshot(self.path, '', 'missing'))
            self.status.set_text(self.external.message(self.dirty))
            self.external_controls.set_visible(True)
            self.keep_button.set_sensitive(False)
            self.update_label()
        self.window.sources.remove(self.watch_timer)
        self.watch_timer = self.window.sources.timeout(250, self.check_external)

    def check_external(self):
        self.watch_timer = None
        if self._shutting_down or self.window._shutting_down or self.disconnected or self.loading: return
        if self.saving or self.watch_busy:
            self.watch_again = True
            self.watch_timer = self.window.sources.timeout(300, self.check_external)
            return
        self.watch_busy = True
        backend, path, generation = self.backend, self.path, getattr(self.backend, 'generation', 0)
        def done(snapshot, error):
            self.watch_busy = False
            if self._shutting_down or self.window._shutting_down or path != self.path or generation != getattr(backend, 'generation', 0): return
            if error: self.status.set_text('External change could not be read: ' + str(error))
            elif self.external.observe(self.revision, snapshot):
                self.status.set_text(self.external.message(self.dirty))
                self.external_controls.set_visible(True)
                self.keep_button.set_sensitive(not self.external.deleted)
                self.update_label()
            else:
                self.external_controls.set_visible(False)
                self.update_label()
            if self.watch_again:
                self.watch_again = False
                self.window.sources.remove(self.watch_timer)
                self.watch_timer = self.window.sources.timeout(250, self.check_external)
        self.window.work(lambda: backend.read(path), done)

    def reload_external(self):
        snapshot = self.external.snapshot
        if snapshot is None or self.external.deleted or self.saving: return
        if self.dirty:
            self.window.confirm_discard('Discard editor changes and reload the external version?', lambda: self.loaded(snapshot))
        else: self.loaded(snapshot)

    def compare_external(self):
        if self.external.snapshot is None: return
        from .diff_model import text_diff
        diff = text_diff(self.external.snapshot.content, self.text(), self.path + ' (external)', self.path + ' (editor)')
        self.window.open_text_view(self.device_id, 'Compare: ' + Path(self.path).name, diff or 'Versions have identical text.', 'diff')

    def keep_external(self):
        snapshot = self.external.snapshot
        if snapshot is None or self.external.deleted or self.saving: return
        def keep():
            if self._shutting_down: return
            self.revision = snapshot.revision
            self.buffer.set_modified(True)
            self.status.set_text('Keeping editor contents. Save replaces the external version only if its revision still matches. Compare remains available.')
        self.window.confirm_action('Keep My Version?', 'A later save will replace the external text. The external snapshot remains available for comparison.', 'Keep My Version', keep)

    def show_search(self): self.editing.show_search()
    def find_next(self, backward=False): self.editing.find_next(backward)

    def close(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.editing.close()
        if self.watch_handle: self.watch_handle.close()
        self.window.sources.remove(self.watch_timer)
        if hasattr(self.window, "code_navigation"):
            self.window.code_navigation.detach(self)
        self.signals.close()
        if self.copy_dialog:
            self.copy_dialog.destroy()
            self.copy_dialog = None
        self.window = None


# Registry name for the existing editor; its editing/save behavior is unchanged.
TextEditorViewer = EditorPane


class NauiWindow(Gtk.ApplicationWindow):
    # Existing navigation code runs against one TerminalState, selected either
    # by the user or temporarily by the origin of a PTY callback. Background
    # terminals never borrow the active terminal's key, cwd or navigation job.
    @property
    def runtime(self):
        return self.runtimes.get(self._event_terminal or self.active_terminal_id)

    @property
    def terminal(self):
        return self.terminals.get(self.runtime.id) if self.runtime else None

    @property
    def explorer(self):
        identity = self.runtime.device_id if self._event_terminal and self.runtime else self.workspace.active_device_id
        return self.workspace_sidebar.explorers[identity]

    @property
    def contexts(self):
        return self.runtime.contexts if self.runtime else {}

    @property
    def active_session(self):
        return self.runtime.active_session if self.runtime else None

    @active_session.setter
    def active_session(self, value):
        if self.runtime:
            self.runtime.active_session = value

    @property
    def at_prompt(self): return self.runtime.at_prompt if self.runtime else False

    @at_prompt.setter
    def at_prompt(self, value):
        if self.runtime:
            self.runtime.at_prompt = value

    @property
    def navigation_pending(self): return self.runtime.navigation_pending if self.runtime else None

    @navigation_pending.setter
    def navigation_pending(self, value):
        if self.runtime:
            self.runtime.navigation_pending = value

    @property
    def navigation_timeout(self): return self.runtime.navigation_timeout if self.runtime else None

    @navigation_timeout.setter
    def navigation_timeout(self, value):
        if self.runtime:
            self.runtime.navigation_timeout = value

    def __init__(self, app, paths, cwd):
        super().__init__(application=app, title="Naui", default_width=1100, default_height=750)
        self._shutting_down = False
        self._discard_approved = False
        self.sources, self.signals = Sources(), Signals()
        self.file_work = FileWork(self.sources)
        self.alive = True
        from .keybindings import CommandRegistry
        self.command_registry = CommandRegistry(WorkspaceStore().path.with_name("keybindings.json"),
            on_error=lambda error: self.set_status("Command failed: " + str(error)))
        self.plugins = PluginManager(self)
        self.plugins.load_bundled()
        self.authentication = AuthenticationManager()
        self._transfers_exit_approved = False
        self.editors, self.sessions = [], {}
        self.viewers, self.pending_paths = [], set()
        self.viewer_registry = ViewerRegistry(self.plugins.features["viewer_specs"])
        self.preview_cache = PreviewCache()
        self.viewer_factories = self.plugins.features["viewer_factories"]
        self.workspace_store = WorkspaceStore()
        config_error = None
        try:
            records = self.workspace_store.load()
        except (OSError, ValueError, UnicodeError) as error:
            records, config_error = [], str(error)
        self.workspace = Workspace(cwd, records)
        from .plugin_catalog import PluginCatalog
        PluginCatalog(self.plugins, safe_mode=getattr(app, 'safe_mode', False))
        from .ai_agents import AiAgentManager
        self.ai_agents = AiAgentManager(self.plugins.catalog.store.directory, self.launch_ai_terminal,
            self.stop_ai_terminal, self.send_ai_input, changed=self.ai_tasks_changed)
        self.plugin_manager_view = None
        self.file_watch = FileWatchService(self.sources, self.workspace.devices.__getitem__)
        self.command_service = CommandService()
        self.pending_runs, self.running_runs = {}, {}
        self.sidebar_sections = {}
        self.ui_settings = UISettings(self.workspace_store.path.with_name('ui.json'))
        from .sidebar_layout import sidebar_width
        self.sidebar_width = sidebar_width(self.ui_settings.width)
        self.sidebar_collapsed = self.ui_settings.values['sidebar_collapsed']
        display = self.get_display()
        monitors = display.get_monitors()
        geometry = monitors.get_item(0).get_geometry() if monitors.get_n_items() else None
        width, height = self.ui_settings.values['window_width'], self.ui_settings.values['window_height']
        self.set_default_size(min(width, geometry.width) if geometry else width,
                              min(height, geometry.height) if geometry else height)
        self.editor_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self.editor_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_editor_font()
        # Invalid existing configuration is preserved until the user explicitly
        # changes the workspace; startup/shutdown must not overwrite it silently.
        self.workspace_dirty = False
        self.transfers = TransferManager(lambda identity: self.workspace.devices[identity])
        for device in self.workspace.devices.values():
            self.bind_authentication(device)
        self.local_backend = self.workspace.devices["local"].filesystem_backend
        self.runtimes, self.terminals, self.terminal_labels = {}, {}, {}
        self.session_devices, self.session_terminals = {}, {}
        self.active_terminal_id, self._event_terminal = None, None
        self.dialog = None
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(self.root)
        toolbar = Gtk.Box(spacing=6)
        toolbar.add_css_class('naui-toolbar')
        self.keybinding_controls = []
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        for text, icon, identity, callback in [
                ("Save", 'document-save-symbolic', 'editor.save', lambda editor: editor.save()),
                ("Undo", 'edit-undo-symbolic', 'editor.undo', lambda editor: editor.buffer.undo() if editor.buffer.get_can_undo() else None),
                ("Redo", 'edit-redo-symbolic', 'editor.redo', lambda editor: editor.buffer.redo() if editor.buffer.get_can_redo() else None),
                ("Find", 'edit-find-symbolic', 'editor.find', lambda editor: editor.show_search()),
                ("Save a copy…", 'document-save-as-symbolic', None, self.save_copy)]:
            button = Gtk.Button(icon_name=icon, tooltip_text=text)
            button.update_property([Gtk.AccessibleProperty.LABEL], [text])
            if identity: self.keybinding_controls.append((button, identity, text))
            self.signals.connect(button, "clicked", lambda button, cb=callback: self.with_editor(cb))
            toolbar.append(button)
        self.root.append(toolbar)
        view_menu = Gtk.MenuButton(label='View')
        view_popover = Gtk.Popover()
        view_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        from .layout_presets import LayoutPresets
        self.layout_presets = LayoutPresets(self)
        view_actions.append(self.layout_presets.button)
        for label, action in [('Command Palette', lambda: self.development.palette()),
                              ('Run Project Task', lambda: self.development.run_task()),
                              ('Save Project Task', lambda: self.development.add_task()),
                              ('Pin or Unpin Tab', lambda: self.notebook.toggle_pin()),
                              ('Reopen Closed File', lambda: self.development.reopen()),
                              ('Toggle Right Panel', lambda: self.right_panels.toggle()),
                              ('Toggle Sidebar', self.toggle_sidebar), ('Reset Layout', self.reset_layout),
                              ('Split Side by Side', lambda: self.split_tabs(Gtk.Orientation.HORIZONTAL)),
                              ('Split Top and Bottom', lambda: self.split_tabs(Gtk.Orientation.VERTICAL)),
                              ('Move Tab to Other Group', self.move_tab_group),
                              ('Single View', lambda: self.notebook.single_view()),
                              ('Zoom In', lambda: self.zoom_active(1)), ('Zoom Out', lambda: self.zoom_active(-1)),
                              ('Reset Zoom', lambda: self.zoom_active(0))]:
            control = Gtk.Button(label=label)
            self.signals.connect(control, 'clicked', lambda *_, action=action: (view_popover.popdown(), action()))
            view_actions.append(control)
            identity = {'Toggle Sidebar':'sidebar.toggle','Zoom In':'view.zoom_in','Zoom Out':'view.zoom_out','Reset Zoom':'view.zoom_reset'}.get(label)
            if identity: self.keybinding_controls.append((control, identity, label))
        view_popover.set_child(view_actions)
        view_menu.set_popover(view_popover)
        toolbar.prepend(view_menu)
        settings_menu = Gtk.MenuButton(label='Settings')
        settings_popover = Gtk.Popover()
        settings_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        for title, action in [('Editor…', lambda: show_editor_settings(self)), ('Keybindings…', self.show_keybindings),
                              ('Plugins…', self.show_plugins), ('About NaUi', self.show_about)]:
            control = Gtk.Button(label=title)
            self.signals.connect(control,'clicked',lambda *_,action=action:(settings_popover.popdown(),action()))
            settings_actions.append(control)
        settings_popover.set_child(settings_actions);settings_menu.set_popover(settings_popover)
        toolbar.append(settings_menu)
        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        for label, icon, identity, action in [
                ('New Terminal', 'tab-new-symbolic', 'terminal.new', self.new_terminal),
                ('Close Terminal', 'window-close-symbolic', 'terminal.close', self.close_active_terminal),
                ('Disconnect', 'network-offline-symbolic', None, self.disconnect_active_device)]:
            control = Gtk.Button(icon_name=icon, tooltip_text=label)
            control.update_property([Gtk.AccessibleProperty.LABEL], [label])
            if identity: self.keybinding_controls.append((control, identity, label))
            self.signals.connect(control, 'clicked', lambda *_, action=action: action())
            toolbar.append(control)
        self.device_indicator = Gtk.Label(xalign=0, ellipsize=3)
        self.device_indicator.set_max_width_chars(40)
        self.device_indicator.set_margin_start(6)
        self.device_indicator.set_hexpand(True)
        self.signals.connect(self.device_indicator, 'notify::label', lambda label, *_: label.set_tooltip_text(label.get_text()))
        toolbar.append(self.device_indicator)
        self.right_panel_button = Gtk.ToggleButton(icon_name='sidebar-show-right-symbolic',
            tooltip_text='Show or hide AI / plugin panel')
        self.right_panel_button.update_property([Gtk.AccessibleProperty.LABEL], ['Show or hide AI / plugin panel'])
        self.signals.connect(self.right_panel_button, 'clicked', lambda *_: self.right_panels.toggle())
        toolbar.append(self.right_panel_button)
        from .tab_workspace import TabWorkspace
        self.notebook = TabWorkspace(self.ui_settings)
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        self.transfer_panel = TransferPanel(self)
        self.workspace_sidebar = self.plugins.features['explorer_factory'](self)
        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        from .sidebar import SidebarManager, ACTIVITIES, empty_panel, settings_panel
        self.left_sidebar = SidebarManager(self)
        self.left_sidebar.set_size_request(220, -1)
        from .search_ui import SearchPanel
        self.search_panel = SearchPanel(self)
        panels = {'explorer': self.workspace_sidebar, 'devices': self.workspace_sidebar.devices_panel,
                  'transfers': self.transfer_panel,
                  'source_control': empty_panel('Source Control', 'Loading repository status…'),
                  'search': self.search_panel,
                  'settings': settings_panel(self, self.left_sidebar)}
        for activity in ACTIVITIES: self.left_sidebar.register_panel(activity, panels[activity.id])
        self.transfer_panel.set_expanded(True)
        split.set_start_child(self.left_sidebar)
        self.content_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True, hexpand=True, vexpand=True)
        from .plugin_panels import RightPanelHost
        self.right_panels = RightPanelHost(self, self.content_split)
        self.signals.connect(self.right_panels, 'notify::visible',
            lambda panel, *_: self.right_panel_button.set_active(panel.get_visible()))
        self.content_split.set_start_child(self.notebook)
        self.content_split.set_end_child(self.right_panels)
        self.content_split.set_resize_start_child(True)
        self.content_split.set_resize_end_child(False)
        self.content_split.set_shrink_start_child(False)
        self.content_split.set_shrink_end_child(False)
        split.set_end_child(self.content_split)
        split.set_resize_start_child(False)
        split.set_shrink_start_child(False)
        split.set_shrink_end_child(False)
        self.split = split
        split.set_position(self.sidebar_width)
        self.left_sidebar.set_visible(not self.sidebar_collapsed)
        self.signals.connect(split, 'notify::position', self.sidebar_resized)
        self.signals.connect(split, 'notify::max-position', self.layout_mapped)
        split.set_vexpand(True)
        self.root.append(split)
        self.signals.connect(self, 'map', self.layout_mapped)
        self.status = Gtk.Label(label="nano FILE opens an editor tab. /usr/bin/nano runs GNU nano.", xalign=0)
        self.status.add_css_class('naui-statusbar')
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(80)
        self.status.set_margin_start(8)
        self.status.set_margin_bottom(6)
        self.root.append(self.status)
        self.code_navigation = self.plugins.features["navigation_factory"](self)
        self.create_terminal(self.workspace.devices["local"], cwd)
        self.show_context()
        self.signals.connect(self.notebook, "switch-page", self.page_switched)
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.signals.connect(controller, "key-pressed", self.key_pressed)
        self.signals.connect(controller, "key-released", lambda _, key, *args: self.notebook.end_cycle() if key in (Gdk.KEY_Control_L, Gdk.KEY_Control_R) else None)
        self.add_controller(controller)
        self.signals.connect(self, "close-request", self.close_requested)
        from .development import Development
        from .session_restore import SessionRestore
        self.development = Development(self)
        self.session_restore = SessionRestore(self)
        self.session_restore.restore()
        for path in paths:
            self.open_path(self.local_backend, resolve_path(cwd, path))
        if not self.editors: self.sources.idle(self.terminal.view.grab_focus)
        selected = self.ui_settings.values['selected_device']
        if selected in self.workspace.devices and selected != 'local':
            self.sources.idle(lambda: self.activate_device(selected, reconnect=False))
        if getattr(app, 'plugin_paths', None):
            self.sources.idle(self.load_user_plugins, list(app.plugin_paths))
        from .keybinding_host import register_core
        register_core(self, self.command_registry)
        self.command_registry.listeners.append(self.update_keybinding_labels)
        self.update_keybinding_labels()
        if self.command_registry.warnings: self.set_status('; '.join(self.command_registry.warnings))
        self.sources.idle(self.restore_user_plugins)
        if getattr(app, 'safe_mode', False):
            self.set_title('Naui — SAFE MODE')
            self.set_status('SAFE MODE: user plugins are disabled for this launch. Saved enable preferences are unchanged.')
        if getattr(app, 'dev_plugins', False): self.sources.idle(self.show_plugins)
        if config_error:
            self.set_status("Workspace config could not be loaded; original file preserved: " + config_error)

    def sidebar_resized(self, pane, *_):
        if self._shutting_down or self.sidebar_collapsed: return
        from .sidebar_layout import sidebar_width
        width = pane.get_position()
        bounded = sidebar_width(width, self.get_width())
        if width != bounded: pane.set_position(bounded)
        self.sidebar_width = bounded
        self.sources.remove(getattr(self, "layout_save_source", None))
        self.layout_save_source = self.sources.timeout(400, self.save_ui)

    def layout_mapped(self, *_):
        if self._shutting_down: return
        # Native pane constraints leave room for main content; transient tiny
        # windows collapse navigation without changing the saved toggle state.
        tiny = 0 < self.get_width() < 600
        self.left_sidebar.set_visible(not self.sidebar_collapsed and not tiny)
        if not tiny and not self.sidebar_collapsed: self.sidebar_resized(self.split)

    def show_sidebar_panel(self, identity):
        if not self._shutting_down: self.left_sidebar.select(identity, focus=True)

    def save_ui(self):
        try:
            self.ui_settings.save(self.sidebar_width, sidebar_collapsed=self.sidebar_collapsed,
                selected_device=self.workspace.active_device_id,
                window_width=max(640, min(3840, self.get_width() or 1100)),
                window_height=max(480, min(2160, self.get_height() or 750)))
        except OSError as error:
            self.set_status('Could not save UI preferences: ' + str(error))

    def toggle_sidebar(self):
        if self._shutting_down: return
        self.sidebar_collapsed = not self.sidebar_collapsed
        self.left_sidebar.set_visible(not self.sidebar_collapsed)
        if not self.sidebar_collapsed: self.split.set_position(self.sidebar_width)
        self.save_ui()

    def split_tabs(self, orientation):
        if not self.notebook.split(orientation):
            self.set_status('Open at least two tabs, then choose a split layout from View.')

    def move_tab_group(self):
        if not self.notebook.move_current():
            self.set_status('Choose a split layout from View first, then move the active tab to the other group.')

    def reset_layout(self):
        if self._shutting_down: return
        self.notebook.single_view()
        try: self.ui_settings.reset()
        except OSError as error: self.set_status(str(error))
        self.sidebar_width, self.sidebar_collapsed = 300, False
        self.left_sidebar.set_visible(True)
        self.split.set_position(300)
        self.left_sidebar.select('explorer')
        self.left_sidebar.scale_control.set_value(100)
        self.left_sidebar.density_control.set_active(0)
        self.left_sidebar.apply_style()
        self.transfer_panel.set_expanded(True)
        for section in self.sidebar_sections.values(): section.set_expanded(True)
        self.workspace_sidebar.reset_sections()
        self.right_panels.apply_layout()
        self.set_default_size(1100, 750)
        self.layout_mapped()

    def apply_editor_font(self):
        css = 'textview.naui-code { font-family: monospace; font-size: ' + str(self.ui_settings.values['editor_font']) + 'pt; }'
        # Native icon buttons retain their actions at the narrowest sidebar
        # width; theme padding must not become a minimum-width constraint.
        css += ' .naui-sidebar button.image-button, .naui-sidebar menubutton > button { min-width: 20px; min-height: 20px; padding: 2px; }'
        # GTK's default top tab strip has -1px/-2px vertical margins. Its
        # zero-height request during empty/tab-removal transitions becomes -3,
        # tripping Gtk's size validation (and fatal-warnings). Keep this strip's
        # geometry nonnegative; do not suppress the native warning handler.
        css += ' notebook.naui-tabs > header > tabs { margin: 0; }'
        css += '.naui-source-control button { min-height: 20px; padding: 1px 3px; } .naui-source-control .highlight { background-color: alpha(@theme_fg_color, 0.12); }'
        css += '''
            .naui-toolbar button { min-height: 26px; padding: 2px 8px; }
            .naui-toolbar button.image-button { min-width: 26px; padding: 2px; }
            notebook.naui-tabs > header { background-color: alpha(@theme_fg_color, 0.03); }
            notebook.naui-tabs > header > tabs > tab { border-radius: 0; padding: 6px 12px; }
            notebook.naui-tabs > header > tabs > tab:checked {
                background-color: alpha(@theme_fg_color, 0.07);
            }
            notebook.naui-tabs.naui-active-group > header > tabs > tab:checked {
                box-shadow: inset 0 2px @theme_selected_bg_color;
            }
            .naui-statusbar { border-top: 1px solid alpha(@theme_fg_color, 0.12); padding-top: 4px; }
        '''
        if hasattr(self.editor_css, 'load_from_string'): self.editor_css.load_from_string(css)
        else: self.editor_css.load_from_data(css.encode())

    def zoom_active(self, direction):
        if self._shutting_down: return
        focus = self.get_focus()
        if focus and (focus is self.left_sidebar or focus.is_ancestor(self.left_sidebar)):
            self.left_sidebar.zoom(direction)
            return
        page = self.notebook.get_nth_page(self.notebook.get_current_page())
        name = 'terminal_font' if isinstance(page, TerminalPane) else 'editor_font' if isinstance(page, (EditorPane, ReadOnlyView)) else None
        if name is None: return
        value = 12 if direction == 0 else max(6, min(32, self.ui_settings.values[name] + direction))
        self.ui_settings.values[name] = value
        if name == 'editor_font': self.apply_editor_font()
        else:
            for terminal in self.terminals.values():
                if not terminal.closed: terminal.view.set_font(Pango.FontDescription.from_string('Monospace ' + str(value)))
        self.save_ui()

    @staticmethod
    def plugin_panel_id(plugin_id, identity):
        if plugin_id == 'base-git' and identity == 'status': return 'source_control'
        import hashlib
        return 'plugin.' + plugin_id + '.' + hashlib.sha256(str(identity).encode()).hexdigest()[:12]

    def plugin_sidebar_badge(self, plugin_id, identity, count):
        if not self._shutting_down:
            self.left_sidebar.set_badge(self.plugin_panel_id(plugin_id, identity), count)

    def plugin_sidebar(self, plugin_id, identity, title, rows):
        if self._shutting_down: return
        key = plugin_id, identity
        if key not in self.sidebar_sections:
            section = self.sidebar_sections[key] = SidebarSection(title)
            section.set_expanded(self.ui_settings.values['source_control_expanded'])
            self.signals.connect(section, 'notify::expanded', lambda section, *_:
                self.ui_settings.values.update(source_control_expanded=section.get_expanded()))
            section.set_vexpand(True)
            section.scroll.set_vexpand(True)
            section.scroll.set_propagate_natural_height(False)
            from .sidebar_layout import Panel
            panel_id = self.plugin_panel_id(plugin_id, identity)
            if panel_id == 'source_control':
                section.add_css_class('naui-source-control')
                self.left_sidebar.replace_panel(panel_id, section)
            else:
                self.left_sidebar.register_panel(Panel(panel_id, title, 'application-x-addon-symbolic'), section)
        api = self.plugins.apis[plugin_id]
        self.sidebar_sections[key].heading.set_text(title)
        self.sidebar_sections[key].heading.set_tooltip_text(title)
        self.sidebar_sections[key].update(rows, lambda: not api.closed and not api.quiescing and not self._shutting_down)

    def remove_plugin_ui(self, plugin_id, views):
        """Called after plugin callbacks quiesce, on a deferred UI turn."""
        if self._shutting_down: return
        if hasattr(self, "right_panels"): self.right_panels.remove_plugin(plugin_id)
        for key, section in list(self.sidebar_sections.items()):
            if key[0] == plugin_id:
                section.close()
                self.signals.disconnect(section)
                panel_id = self.plugin_panel_id(*key)
                if panel_id == 'source_control':
                    from .sidebar import empty_panel
                    self.left_sidebar.replace_panel(panel_id, empty_panel('Source Control', 'Git plugin is disabled.'))
                    self.left_sidebar.set_badge(panel_id, 0)
                else: self.left_sidebar.remove_panel(panel_id)
                del self.sidebar_sections[key]
        for view in views:
            self.remove_plugin_view(view)

    def remove_plugin_view(self, view):
        if self._shutting_down or view not in self.viewers: return
        # BrowserHandle.stop() may already have quiesced this view. Its closed
        # flag must not prevent removing the still-owned notebook page.
        if hasattr(self, 'tab_menu') and self.tab_menu.page is view: self.tab_menu.dismiss()
        view.close()
        page = self.notebook.page_num(view)
        if page >= 0: self.notebook.remove_page(page)
        self.viewers.remove(view)

    def open_text_view(self, device_id, title, content, language='diff'):
        if self._shutting_down: return
        if len(content.encode('utf-8')) > 8 * 1024 * 1024:
            self.set_status('Diff exceeds the 8 MiB view limit; editor versions remain preserved.')
            return
        if len(self.viewers) >= 64:
            self.set_status('Close a view before opening another (64-tab limit).')
            return
        device = self.workspace.devices.get(device_id)
        if device is None:
            origin = next((view for view in self.viewers if view.device_id == device_id), None)
            if origin is None: return
            backend, session = origin.backend, origin.session
            owner = getattr(getattr(backend, 'connection', None), 'host', 'Removed device')
        else:
            owner, backend, session = device.display_name, device.filesystem_backend, device.session_id
        if language == 'diff' and ('@@ ' in content or 'diff --git ' in content or 'Binary files ' in content):
            from .diff_view import DiffView
            viewer = DiffView(device_id, owner, title, content, context={'historical': True})
        else:
            viewer = ReadOnlyView(device_id, owner, title, content, language)
        viewer.backend, viewer.session = backend, session
        self._add_viewer(viewer)
        return viewer

    def open_diff_view(self, device_id, title, patch, *, open_file=None, context=None):
        if self._shutting_down or len(self.viewers) >= 64:
            self.set_status('Cannot open another view.'); return
        if len(patch.encode()) > 8 * 1024 * 1024:
            self.set_status('Diff exceeds 8 MiB; open the file in the editor.'); return
        from .diff_view import DiffView
        device = self.workspace.devices[device_id]
        viewer = DiffView(device_id, device.display_name, title, patch, open_file=open_file, context=context)
        viewer.backend, viewer.session = device.filesystem_backend, device.session_id
        self._add_viewer(viewer)
        return viewer

    def open_browser_view(self, device_id, title):
        if self._shutting_down or len(self.viewers) >= 64: raise RuntimeError('Cannot open another view')
        device = self.workspace.devices[device_id]
        viewer = BrowserView(device_id, device.display_name, title)
        viewer.backend, viewer.session = device.filesystem_backend, device.session_id
        self._add_viewer(viewer)
        return viewer

    def plugin_input(self, title, fields, callback):
        if self._shutting_down or self.dialog: return
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dialog.add_button('Run', Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        entries = []
        for label, value in fields:
            dialog.get_content_area().append(Gtk.Label(label=label, xalign=0))
            entry = Gtk.Entry(text=value)
            entry.set_hexpand(True)
            dialog.get_content_area().append(entry); entries.append(entry)
        self.dialog = dialog
        def response(dialog, answer):
            values = [entry.get_text() for entry in entries]
            # Arguments are not persisted in workspace/settings/history by Naui.
            for entry in entries: entry.set_text('')
            self.dialog = None
            self.signals.disconnect(dialog); dialog.destroy()
            if not self._shutting_down and answer == Gtk.ResponseType.ACCEPT: callback(values)
        self.signals.connect(dialog, 'response', response)
        dialog.present()

    def queue_run(self, device_id, cwd, command, new_terminal=False):
        if self._shutting_down: raise RuntimeError('Naui is closing')
        from .runner_commands import terminal_command
        text = terminal_command(cwd, command)
        device = self.workspace.devices[device_id]
        runtime = self.device_terminal(device_id)
        if device.type == 'ssh' and new_terminal:
            self.connect_device(device, new_terminal=True)
            runtime = self.device_terminal(device_id)
        elif device.type == 'ssh' and not device.connected:
            self.activate_device(device_id)
            runtime = self.device_terminal(device_id)
        elif device.type == 'local' and (new_terminal or runtime is None):
            runtime = self.create_terminal(device, cwd)
        elif runtime and not runtime.at_prompt:
            raise RuntimeError('The device terminal is busy. Wait for its prompt or use a new local terminal.')
        if runtime is None: raise RuntimeError('No terminal is available for this device')
        if runtime.id in self.pending_runs: raise RuntimeError('A run is already pending for this terminal')
        identity = uuid.uuid4().hex
        self.pending_runs[runtime.id] = {'id': identity, 'device_id': device_id, 'cwd': cwd, 'text': text}
        self.active_terminal_id = runtime.id
        self.workspace.active_device_id = device_id
        terminal = self.terminals[runtime.id]
        self.notebook.set_current_page(self.notebook.page_num(terminal))
        terminal.view.grab_focus()
        self.in_terminal(runtime.id, self.prepare_run)
        return identity

    def prepare_run(self):
        if self._shutting_down or not self.runtime: return
        request = self.pending_runs.get(self.runtime.id)
        if not request or self.runtime.closed or self.runtime.device_id != request['device_id']: return
        device = self.workspace.devices[request['device_id']]
        if not device.connected or not self.at_prompt or self.navigation_pending: return
        self.navigation_pending = {'session': self.active_session, 'target': request['cwd'], 'ls': False,
                                   'phase': 'prepare', 'previous': self.contexts[self.active_session]['path'], 'run': request}
        self.at_prompt = False
        self.terminal.send_input(PREPARE_RUN)
        identity, pending = self.runtime.id, self.navigation_pending
        def timeout():
            if self.navigation_pending is pending:
                self.pending_runs.pop(identity, None)
                self.cancel_navigation()
                self.set_status('Run cancelled: shell did not acknowledge readiness. Press Enter to resynchronize.')
        self.navigation_timeout = self.sources.timeout(5000, self.in_terminal, identity, timeout)

    def interrupt_run(self, identity):
        if self._shutting_down: return
        run = self.running_runs.get(identity)
        if not run: return
        terminal_id, device_id = run
        runtime = self.runtimes.get(terminal_id)
        if runtime and not runtime.closed and runtime.device_id == device_id and not runtime.at_prompt:
            self.terminals[terminal_id].send_input(b'\x03')

    def bind_authentication(self, device):
        if device.type == 'ssh':
            device.filesystem_backend.authentication = self.authentication
            device.filesystem_backend.device_id = device.id

    def new_terminal(self, device_id=None):
        if self._shutting_down:
            return
        device = self.workspace.devices[device_id or self.workspace.active_device_id]
        if device.type == 'ssh' and device_id is None:
            if self.dialog: return
            dialog = Gtk.Dialog(title='New terminal', transient_for=self, modal=True)
            dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
            dialog.add_button('Local terminal', 1)
            dialog.add_button('SSH terminal', 2)
            label = Gtk.Label(label='SSH: ' + device.display_name, ellipsize=Pango.EllipsizeMode.END, max_width_chars=45)
            label.set_tooltip_text(device.display_name)
            dialog.get_content_area().append(label)
            self.dialog = dialog
            def chosen(dialog, response):
                self.dialog = None
                dialog.destroy()
                if response in (1, 2) and not self._shutting_down:
                    self.new_terminal('local' if response == 1 else device.id)
            dialog.connect('response', chosen)
            dialog.present()
            return
        if device.type == 'ssh':
            self.connect_device(device, new_terminal=True)
            return
        cwd = device.navigation_state.current_path
        self.create_terminal(device, cwd if cwd and os.path.isdir(cwd) else os.path.expanduser('~'))

    def close_active_terminal(self):
        if self.active_terminal_id:
            self.close_terminal(self.active_terminal_id)

    def close_terminal(self, identity, force=False):
        if self._shutting_down or identity not in self.terminals:
            return
        terminal = self.terminals[identity]
        runtime = self.runtimes[identity]
        if runtime.closing:
            return
        idle_remote = self.workspace.devices[runtime.device_id].type == 'ssh' and runtime.at_prompt
        foreground = terminal.bridge.foreground_description() if not terminal.closed and not idle_remote else None
        if foreground and not force:
            self.confirm_action('A process is still running in this terminal', foreground,
                                'Close Terminal', lambda: self.close_terminal(identity, True))
            return
        # A tab button's clicked signal (or a PTY callback) is still on the
        # native stack here. Never unparent/finalize its VTE/page in that stack.
        # Quiesce application callbacks now; dispose on the next main-loop turn.
        runtime.closing = True
        self.code_navigation.remove_terminal(identity)
        self.signals.disconnect(terminal.tab_close_button)
        self.sources.remove(runtime.navigation_timeout)
        runtime.navigation_timeout = runtime.navigation_pending = None
        self.sources.idle(self.dispose_terminal, identity)

    def dispose_terminal(self, identity):
        if self._shutting_down or identity not in self.terminals:
            return
        terminal, runtime = self.terminals[identity], self.runtimes[identity]
        if hasattr(self, 'pending_runs'):
            self.pending_runs.pop(identity, None)
            self.running_runs = {key: value for key, value in self.running_runs.items() if value[0] != identity}
        runtime = self.runtimes[identity]
        for session, owner in list(self.session_terminals.items()):
            if owner == identity:
                self.disconnect_session(session)
        terminal.close()
        agents = getattr(self, 'ai_agents', None)
        if agents: agents.terminal_finished(identity, terminal.bridge.process.returncode, removed=True)
        runtime.closed = True
        page = self.notebook.page_num(terminal)
        # Remove mappings BEFORE GTK emits switch-page. The removed session is
        # no longer a candidate for focus, filesystem events or navigation.
        del self.terminals[identity], self.terminal_labels[identity]
        for device in self.workspace.devices.values():
            if identity in device.terminal_sessions:
                device.terminal_sessions.remove(identity)
            if device.active_terminal_id == identity:
                device.active_terminal_id = None
        if self.active_terminal_id == identity:
            self.active_terminal_id = next((key for key, state in self.runtimes.items()
                                           if not state.closed and key in self.terminals), None)
        if page >= 0:
            self.notebook.remove_page(page)
        # Keep the plain exited runtime until any signed late OSC/idle event has
        # drained. It owns no PTY/widget and closed guards ignore such events.
        self.update_workspace_ui()
        if not any(not state.closed for state in self.runtimes.values()):
            self.sources.idle(self.last_terminal_closed)

    def last_terminal_closed(self):
        if self._shutting_down or any(not state.closed for state in self.runtimes.values()):
            return
        if self.dialog:
            self.sources.timeout(100, self.last_terminal_closed)
            return
        # Explicit tab closing is not a shell `exit`; leave GUI/editor work open.
        self.create_terminal(self.workspace.devices['local'], os.path.expanduser('~'))

    def after_terminal_exit(self, identity):
        if self._shutting_down or identity not in self.terminals:
            return
        runtime = self.runtimes[identity]
        device = self.workspace.devices[runtime.base_device_id]
        decision = classify_exit(remote=device.type == 'ssh', returncode=self.terminals[identity].returncode,
                                 other_terminals=any(not state.closed for key, state in self.runtimes.items() if key != identity))
        if decision.action == 'close_tab':
            self.set_status(decision.message)
            self.close_terminal(identity, True)
            return
        if self.dialog:
            self.sources.timeout(100, self.after_terminal_exit, identity)
            return
        dialog = Gtk.Dialog(title='Exit Naui?' if decision.action == 'confirm_exit' else 'Terminal exited',
                            transient_for=self, modal=True)
        dialog.get_content_area().append(Gtk.Label(label=decision.message, wrap=True))
        dialog.add_button('Start a new terminal [N]', Gtk.ResponseType.CANCEL)
        dialog.add_button('Exit Naui [Y]' if decision.action == 'confirm_exit' else 'Close Tab', Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        self.dialog = dialog
        def response(dialog, answer):
            if self._shutting_down:
                return
            self.dialog = None
            self.signals.disconnect(dialog)
            self.signals.disconnect(keys)
            dialog.destroy()
            if answer == Gtk.ResponseType.ACCEPT and decision.action == 'confirm_exit':
                self.close()
            elif answer == Gtk.ResponseType.ACCEPT:
                self.close_terminal(identity, True)
            else:
                self.close_terminal(identity, True)
                cwd = self.workspace.devices['local'].navigation_state.current_path
                self.create_terminal(self.workspace.devices['local'], cwd if cwd and os.path.isdir(cwd) else os.path.expanduser('~'))
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        def pressed(controller, key, code, modifiers):
            if key in (Gdk.KEY_y, Gdk.KEY_Y) and decision.action == 'confirm_exit':
                dialog.response(Gtk.ResponseType.ACCEPT)
                return True
            if key in (Gdk.KEY_n, Gdk.KEY_N, Gdk.KEY_Escape):
                dialog.response(Gtk.ResponseType.CANCEL)
                return True
            return False
        self.signals.connect(keys, 'key-pressed', pressed)
        dialog.add_controller(keys)
        self.signals.connect(dialog, 'response', response)
        dialog.present()

    def disconnect_active_device(self):
        device = self.workspace.devices[self.workspace.active_device_id]
        if device.type != 'ssh':
            self.set_status('Disconnect applies only to SSH devices.')
            return
        self.confirm_action('Disconnect ' + device.display_name + '?',
                            'This ends its Naui SSH terminals and cancels its transfers. Unsaved editor contents remain open.',
                            'Disconnect', lambda: self.disconnect_device(device.id))

    def device_properties(self, identity):
        device = self.workspace.devices.get(identity)
        if self._shutting_down or device is None: return
        if self.dialog:
            self.dialog.present()
            return
        from .sshfs import MODES
        backend = device.filesystem_backend
        dialog = Gtk.Dialog(title='SSH Device Properties', transient_for=self, modal=True)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dialog.add_button('Apply', Gtk.ResponseType.ACCEPT)
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.append(Gtk.Label(label=device.display_name + '\n' + device.hostname + '\nState: ' + device.state))
        content.append(Gtk.Label(label='Remote Filesystem'))
        mode = Gtk.DropDown.new_from_strings(['Automatic', 'SSHFS (fallback on failure)', 'Compatibility / SSH'])
        mode.set_selected(MODES.index(backend.filesystem_mode))
        content.append(mode)
        label = Gtk.Label(label=backend.filesystem_status, wrap=True, max_width_chars=55)
        content.append(label)
        content.append(Gtk.Label(label='Optional Linux dependency: sshfs and usable FUSE.\n'
                                       'Only pinned roots are mounted, on demand.\n'
                                       'Git, runner, watchers and protected writes stay remote.', wrap=True))
        self.dialog = dialog
        def response(dialog, answer):
            selected = MODES[mode.get_selected()]
            self.dialog = None
            self.signals.disconnect(dialog)
            dialog.destroy()
            if self._shutting_down or answer != Gtk.ResponseType.ACCEPT: return
            if selected == backend.filesystem_mode: return
            self.code_navigation.remove_device(device.id)
            # Native FUSE teardown can wait; it must not block the UI thread.
            self.work(lambda: backend.set_filesystem_mode(selected),
                      lambda result, error: self._filesystem_mode_changed(device, error))
        self.signals.connect(dialog, 'response', response)
        dialog.present()

    def _filesystem_mode_changed(self, device, error):
        if self._shutting_down or self.workspace.devices.get(device.id) is not device: return
        if error:
            self.set_status(str(error))
            return
        self.persist_workspace()
        for editor in self.editors:
            if editor.device_id == device.id:
                self.code_navigation.loaded(editor)
        self.set_status(device.filesystem_backend.filesystem_status)

    def remove_device_dialog(self, identity):
        device = self.workspace.devices.get(identity)
        if self._shutting_down or device is None or device.type != 'ssh': return
        self.confirm_action('Remove SSH device from workspace?', device.display_name + '\n' + device.hostname
            + '\nThis disconnects Naui sessions and cancels transfers. Unsaved documents stay open.'
            + '\nThis does not modify or shut down the remote machine.',
            'Disconnect and Remove' if device.state != 'disconnected' else 'Remove',
            lambda: self.remove_device(identity))

    def remove_device(self, identity):
        device = self.workspace.devices.get(identity)
        if self._shutting_down or device is None or device.type != 'ssh': return
        self.disconnect_device(identity)
        # PTY pages are disposed by an idle source before workspace removal.
        self.sources.idle(self.finish_device_removal, identity)

    def finish_device_removal(self, identity):
        device = self.workspace.devices.get(identity)
        if self._shutting_down or device is None: return
        owners = [state for state in self.runtimes.values() if not state.closed and state.device_id == identity]
        if any(not state.closing for state in owners):
            self.set_status('Close or exit the remaining SSH terminal before removing this device.')
            return
        if owners:
            self.sources.timeout(30, self.finish_device_removal, identity)
            return
        self.file_watch.remove_device(identity)
        for viewer in self.viewers:
            if viewer.device_id == identity:
                viewer.mark_disconnected('Device removed from workspace; contents preserved. Save a local copy to keep your work.')
                if isinstance(viewer, EditorPane): self.code_navigation.detach(viewer)
        self.code_navigation.remove_device(identity)
        self.workspace.active_device_id = 'local'
        self.workspace_sidebar.remove_device(identity)
        del self.workspace.devices[identity]
        self.activate_device('local')
        self.persist_workspace()
        self.update_workspace_ui()
        self.set_status('Removed ' + device.display_name + ' from this workspace. Remote files are unchanged.')

    def disconnect_device(self, identity):
        if self._shutting_down or identity not in self.workspace.devices:
            return
        device = self.workspace.devices[identity]
        if device.type != 'ssh':
            return
        if hasattr(self, 'search_panel'):
            self.search_panel.service.cancel_device(identity)
        self.transfers.cancel_device(identity)
        self.authentication.forget_device(identity)
        # Only PTYs actually running this device's SSH session are terminated;
        # historical membership must not kill a local shell that already returned.
        incomplete = False
        owners = [key for key, state in self.runtimes.items() if not state.closed and state.device_id == identity]
        for session, owner in list(self.session_terminals.items()):
            if self.session_devices.get(session) == identity:
                self.disconnect_session(session, 'Device disconnected; unsaved contents preserved.')
        device.filesystem_backend.close()
        if device.filesystem_backend.needs_mount_cleanup:
            self.work(device.filesystem_backend.cleanup_mounts,
                      lambda result, error: self.set_status(str(error)) if error else None)
        device.state = 'disconnected'
        for owner in owners:
            runtime = self.runtimes[owner]
            if self.workspace.devices[runtime.base_device_id].type == 'local':
                try:
                    self.terminals[owner].bridge.stop_ssh_foreground(device.hostname)
                except (OSError, RuntimeError) as error:
                    self.set_status(str(error))
                    incomplete = True
                    continue  # Do not terminate a different new local program.
                runtime.active_session, runtime.at_prompt = None, False
                runtime.device_id = runtime.base_device_id
                runtime.contexts[None]['state'] = 'connected'
                if owner in device.terminal_sessions:
                    device.terminal_sessions.remove(owner)
                if device.active_terminal_id == owner:
                    device.active_terminal_id = None
                if self.active_terminal_id == owner:
                    self.workspace.active_device_id = runtime.device_id
                self.in_terminal(owner, self.show_context)
            else:
                self.close_terminal(owner, True)
        for viewer in self.viewers:
            if viewer.device_id == identity:
                viewer.mark_disconnected('Device disconnected; unsaved contents preserved.')
        self.workspace_sidebar.explorers[identity].set_context(device.filesystem_backend, device.session_id,
            device.navigation_state.current_path, device.navigation_state.home, device.state)
        self.update_workspace_ui()
        self.set_status(('SSH foreground changed; filesystem disconnected. Use exit or close its terminal tab. ' if incomplete else 'Disconnected from ' + device.display_name + '. ') + 'Remote machine remains running.')

    def show_plugins(self):
        if self._shutting_down: return
        view = self.plugin_manager_view
        if view is None or view._shutting_down:
            from .plugin_manager_ui import PluginManagerView
            view = self.plugin_manager_view = PluginManagerView(self, developer=getattr(self.get_application(), 'dev_plugins', False))
            # Plugin commands retain the document/terminal they act on.
            if isinstance(view.return_page, EditorPane): self.notebook.keep_open(view.return_page)
            self._add_viewer(view)
        else:
            current = self.notebook.get_nth_page(self.notebook.get_current_page())
            if current is not view:
                view.return_page = current
                if isinstance(current, EditorPane): self.notebook.keep_open(current)
            view.rediscover()
            self.notebook.set_current_page(self.notebook.page_num(view))

    def launch_ai_terminal(self, task, command):
        runtime = self.create_terminal(self.workspace.devices['local'], task.cwd, command)
        runtime.contexts[None]['path'] = task.cwd
        self.terminal_labels[runtime.id].set_text('AI TASK — ' + task.plugin_id)
        return runtime.id, self.terminals[runtime.id].bridge.process

    def stop_ai_terminal(self, identity):
        if identity is None or identity not in self.terminals: return False
        if self._shutting_down:
            self.terminals[identity].close()  # Still parented; normal idempotent PTY cleanup.
            return True
        self.close_terminal(identity, True)
        return False  # Disposal is deferred; its completion owns task cleanup.

    def send_ai_input(self, identity, data):
        if not self._shutting_down and identity in self.terminals:
            self.terminals[identity].send_input(data)

    def ai_tasks_changed(self):
        if self._shutting_down: return
        view = getattr(self, 'plugin_manager_view', None)
        if view and not view._shutting_down: self.sources.idle(view.refresh)

    def restore_user_plugins(self):
        if self._shutting_down: return
        catalog = self.plugins.catalog
        for identity in catalog.restore_candidates():
            # Only plain path/validator data enter workers. Importing trusted
            # startup plugins is off-thread in a separate validation process;
            # actual UI activation happens here on main-thread completion.
            directory = str(catalog.records[identity].directory)
            saved = dict(catalog.store.data['plugins'][identity])
            validator = catalog.validator
            def inspect(directory=directory, saved=saved, validator=validator):
                from .plugin_validation import ValidationResult
                from .plugin_manifest import fingerprint
                if fingerprint(directory) != saved['fingerprint']:
                    return ValidationResult(errors=['Plugin changed since approval; review, validate and enable again'])
                return validator.validate(directory, execute=True)
            def done(result, error, identity=identity, saved=saved):
                if self._shutting_down: return
                # A user may disable/uninstall/enable the plugin while its
                # startup validation is pending. Never undo that later choice.
                if (identity not in catalog.records or identity in self.plugins.plugins
                        or catalog.store.data['plugins'].get(identity) != saved): return
                if error:
                    catalog.startup_finished(identity, failed=True)
                    catalog.records[identity].state = 'failed'
                    catalog.records[identity].error = str(error)
                    self.set_status('Plugin restore failed: ' + str(error))
                    self.plugins.changed(); return
                catalog.accept_validation(identity, result)
                if result.ok:
                    try:
                        catalog.startup_begin(identity)
                        catalog.enable(identity, saved['grants'], persist=False)
                        catalog.startup_finished(identity)
                    except (Exception, SystemExit) as failure:
                        catalog.startup_finished(identity, failed=True)
                        self.set_status('Plugin restore failed: ' + str(failure))
                else:
                    catalog.startup_finished(identity, failed=True)
                    self.set_status('Plugin needs review: ' + identity)
                self.plugins.changed()
            self.work(inspect, done)

    def load_user_plugins(self, paths):
        if self._shutting_down or not paths or self.plugins.catalog.safe_mode:
            return
        if self.dialog:
            self.sources.timeout(100, self.load_user_plugins, paths)
            return
        path, remaining = paths[0], paths[1:]
        try:
            spec, entry = self.plugins.inspect_user(path)
        except (ValueError, OSError) as error:
            self.set_status(str(error))
            self.sources.idle(self.load_user_plugins, remaining)
            return
        def load():
            validator, directory = self.plugins.catalog.validator, str(entry.parent)
            def validated(result, error):
                if self._shutting_down: return
                try:
                    if error: raise error
                    if not result.ok: raise ValueError(result.text())
                    from .plugin_manifest import fingerprint
                    if fingerprint(directory) != result.fingerprint:
                        raise ValueError('Plugin changed after validation; review again')
                    self.plugins.load_user(path, spec.permissions)
                    self.plugins.catalog.discover()
                    self.plugins.catalog.accept_validation(spec.id, result)
                    self.plugins.changed()
                    self.set_status('Enabled user plugin: ' + spec.name)
                except (Exception, SystemExit) as failure:
                    self.set_status('Plugin failed: ' + str(failure)[:1000])
                self.sources.idle(self.load_user_plugins, remaining)
            self.work(lambda: validator.validate(directory, execute=True), validated)
        self.confirm_action('Trust Python plugin: ' + spec.name + '?',
                            'In-process Python is not sandboxed. Enable only trusted code. API permissions: ' +
                            ', '.join(sorted(spec.permissions)) + '\nDirectory: ' + str(entry.parent), 'Trust and Enable', load)

    def create_terminal(self, device, cwd=None, command=None):
        if device.type == "local":
            device.state = "connected"
        identity = uuid.uuid4().hex
        path = device.navigation_state.current_path
        context = {"backend": device.filesystem_backend, "path": path,
                   "home": device.navigation_state.home, "state": device.state, "device_id": device.id}
        state = TerminalState(identity, device.id, device.id, {None: context})
        self.runtimes[identity] = state
        device.terminal_sessions.append(identity)
        device.active_terminal_id = identity
        self.active_terminal_id = identity
        terminal = TerminalPane(cwd or os.path.expanduser("~"),
                                lambda message: self.terminal_message(identity, message),
                                lambda text: self.terminal_status(identity, text),
                                lambda: self.terminal_exit(identity), command=command)
        terminal.device_id = device.id
        terminal.device_label = device.display_name
        self.terminals[identity] = terminal
        terminal.view.set_font(Pango.FontDescription.from_string('Monospace ' + str(self.ui_settings.values['terminal_font'])))
        self.code_navigation.add_terminal(identity, terminal)
        label = Gtk.Label(label="Terminal — " + (device.display_name if device.type == "ssh" else "LOCAL"))
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(28)
        label.set_tooltip_text(label.get_text())
        self.signals.connect(label, 'notify::label', lambda label, *_: label.set_tooltip_text(label.get_text()))
        tab = Gtk.Box(spacing=4)
        tab.append(label)
        close = Gtk.Button.new_from_icon_name('window-close-symbolic')
        close.set_has_frame(False)
        close.set_tooltip_text('Close only this terminal')
        self.signals.connect(close, 'clicked', lambda *_: self.close_terminal(identity))
        terminal.tab_close_button = close
        tab.append(close)
        self.terminal_labels[identity] = label
        self.notebook.append_page(terminal, tab)
        self.notebook.set_current_page(self.notebook.page_num(terminal))
        return state

    def terminal_message(self, identity, message):
        if self._shutting_down or identity not in self.runtimes or self.runtimes[identity].closed or self.runtimes[identity].closing:
            return
        self.in_terminal(identity, self.on_message, message)

    def in_terminal(self, identity, callback, *args):
        if self._shutting_down:
            return
        previous = self._event_terminal
        self._event_terminal = identity
        try:
            callback(*args)
        finally:
            self._event_terminal = previous

    def terminal_status(self, identity, text):
        if self._shutting_down or identity not in self.runtimes or self.runtimes[identity].closing:
            return
        device = self.workspace.devices[self.runtimes[identity].device_id]
        self.set_status(device.display_name + ": " + text)

    def terminal_exit(self, identity):
        if self._shutting_down or self.runtimes[identity].closed or self.runtimes[identity].closing:
            return
        agents = getattr(self, 'ai_agents', None)
        if agents and agents.terminal_finished(identity, self.terminals[identity].returncode):
            self.runtimes[identity].closed = True
            # Retain the closed, read-only VTE page so task output can be
            # inspected. Explicit tab close uses normal deferred disposal.
            self.set_status('AI task finished. Review changes and validate before enabling the plugin.')
            return
        previous = self._event_terminal
        self._event_terminal = identity
        try:
            self.runtime.closed = True
            self.terminal_exited()
            self.sources.idle(self.after_terminal_exit, identity)
        finally:
            self._event_terminal = previous

    def page_switched(self, notebook, page, position):
        if self._shutting_down:
            return
        for identity, terminal in self.terminals.items():
            if terminal is page:
                self.active_terminal_id = identity
                runtime = self.runtimes[identity]
                self.workspace.active_device_id = runtime.device_id
                device = self.workspace.devices[runtime.device_id]
                owns_transport = (device.type == "local" or device.session_id is None
                                  or self.session_is_current(runtime.active_session))
                if not runtime.closed and owns_transport:
                    device.active_terminal_id = identity
                    if device.type == 'ssh' and runtime.active_session:
                        device.session_id = runtime.active_session
                self.in_terminal(identity, self.show_context)
                return
        identity = getattr(page, "device_id", None)
        if identity in self.workspace.devices:
            self.workspace.active_device_id = identity
            terminal = self.device_terminal(identity)
            if terminal:
                self.active_terminal_id = terminal.id
            self.update_workspace_ui()

    def device_terminal(self, identity):
        device = self.workspace.devices[identity]
        ordered = ([device.active_terminal_id] if device.active_terminal_id else []) + list(reversed(device.terminal_sessions))
        return next((self.runtimes[item] for item in ordered
                     if item in self.runtimes and self.runtimes[item].device_id == identity
                     and not self.runtimes[item].closed and not self.runtimes[item].closing), None)

    def activate_device(self, identity, reconnect=True):
        if self._shutting_down:
            return False
        device = self.workspace.devices[identity]
        self.workspace.active_device_id = identity
        runtime = self.device_terminal(identity)
        if device.type == "ssh" and device.state == "disconnected" and reconnect:
            self.connect_device(device)
            return True
        if runtime is None and device.type == 'ssh' and reconnect:
            # Mounted filesystem access can outlive every interactive tab.
            # Never navigate/run through whichever local terminal has focus.
            self.connect_device(device, new_terminal=True)
            return True
        if runtime is None and device.type == "local":
            cwd = device.navigation_state.current_path
            runtime = self.create_terminal(device, cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~"))
        if runtime:
            self.active_terminal_id = runtime.id
            device.active_terminal_id = runtime.id
            if device.type == 'ssh' and runtime.active_session:
                device.session_id = runtime.active_session
            terminal = self.terminals[runtime.id]
            self.notebook.set_current_page(self.notebook.page_num(terminal))
            terminal.view.grab_focus()
            self.in_terminal(runtime.id, self.show_context)
        self.update_workspace_ui()
        return runtime is not None

    def connect_device(self, device, new_terminal=False):
        if self._shutting_down or device.type != "ssh":
            return
        if not new_terminal and device.state == "connecting" and self.device_terminal(device.id):
            self.activate_device(device.id, reconnect=False)
            return
        if not device.connected:
            device.state = "connecting"
        self.workspace.active_device_id = device.id
        runtime = self.create_terminal(device, command=[sys.executable, "-m", "naui.shell", "ssh", *device.reconnect_args()])
        runtime.resume_path = device.navigation_state.current_path
        self.update_workspace_ui()
        self.set_status("Connecting to " + device.display_name + "; authentication happens in its terminal.")

    def add_ssh_device(self, host, name=None):
        device = self.workspace.add_ssh(host, name)
        self.bind_authentication(device)
        self.workspace_sidebar.add_device(device)
        self.persist_workspace()
        self.connect_device(device)
        return device

    def add_workspace_root(self, identity, path):
        device = self.workspace.devices[identity]
        def add():
            device.add_root(path)
            self.workspace_sidebar.explorers[identity].refresh()
            self.persist_workspace()
        if not device.connected:
            add()  # A disconnected root is a reference, validated on connection.
            return
        backend = device.filesystem_backend
        def checked(entry, error):
            if self._shutting_down:
                return
            if error:
                self.set_status(str(error))
            elif not entry.is_directory:
                self.set_status("Workspace roots must be directories.")
            else:
                try:
                    add()
                except ValueError as error:
                    self.set_status(str(error))
        self.work(lambda: backend.properties(path), checked)

    def remove_workspace_root(self, identity, path):
        if self._shutting_down:
            return
        self.workspace.devices[identity].remove_root(path)
        device = self.workspace.devices[identity]
        if device.type == 'ssh':
            self.work(device.filesystem_backend.cleanup_removed_mounts, lambda result, error: None)
        self.workspace_sidebar.explorers[identity].refresh()
        self.persist_workspace()

    def persist_workspace(self):
        if self._shutting_down:
            return
        self.workspace_dirty = True
        try:
            self.workspace_store.save(self.workspace)
            self.workspace_dirty = False
        except (OSError, ValueError) as error:
            self.set_status("Workspace could not be saved: " + str(error))

    def update_workspace_ui(self):
        if self._shutting_down:
            return
        self.workspace_sidebar.update_devices()
        if hasattr(self, 'search_panel'):
            self.search_panel.workspace_changed()
        device = self.workspace.devices[self.workspace.active_device_id]
        self.device_indicator.set_text(("LOCAL" if device.type == "local" else "SSH") + " — "
                                       + device.display_name + " — " + device.state)
        for identity, label in self.terminal_labels.items():
            runtime = self.runtimes[identity]
            self.terminals[identity].device_id = runtime.device_id
            agents = getattr(self, 'ai_agents', None)
            if agents and identity in agents.by_terminal:
                task = agents.tasks[agents.by_terminal[identity]]
                label.set_text('AI TASK — ' + task.plugin_id + ' — ' + task.state)
                continue
            owner = self.workspace.devices[runtime.device_id]
            inactive = owner.type == "ssh" and runtime.active_session and not self.session_is_current(runtime.active_session)
            label.set_text("Terminal — " + (owner.display_name if owner.type == "ssh" else "LOCAL")
                           + (" [exited]" if runtime.closed else " [inactive SSH]" if inactive else ""))
        if hasattr(self, 'plugins'):
            self.plugins.emit('workspace')

    def session_is_current(self, session):
        identity = self.session_devices.get(session)
        device = self.workspace.devices.get(identity)
        return device is not None and device.filesystem_backend.has_session(session)

    def register_remote(self, session, connection):
        username, port = connection_metadata(connection)
        runtime = self.runtime
        candidate = self.workspace.devices[runtime.device_id]
        if not (candidate.type == "ssh" and candidate.hostname == connection.host
                and candidate.username == username and candidate.port == port):
            candidate = next((device for device in self.workspace.devices.values()
                              if device.type == "ssh" and device.hostname == connection.host
                              and device.username == connection.username and device.port == port
                              ), None)
            if candidate is None:
                candidate = self.workspace.add_ssh(connection.host)
                self.workspace_sidebar.add_device(candidate)
        self.bind_authentication(candidate)
        candidate.filesystem_backend.connect(connection, session)
        candidate.username, candidate.port = username, port
        candidate.state = 'connected' if any(
            self.session_is_current(key) and self.runtimes[self.session_terminals[key]].contexts[key]['state'] == 'connected'
            for key, identity in self.session_devices.items() if identity == candidate.id) else 'connecting'
        candidate.session_id = session
        self.sessions[session] = candidate.filesystem_backend
        self.session_devices[session], self.session_terminals[session] = candidate.id, runtime.id
        self.contexts[session] = {"backend": candidate.filesystem_backend,
                                  "path": candidate.navigation_state.current_path,
                                  "home": candidate.navigation_state.home,
                                  "state": "connecting", "device_id": candidate.id}
        if runtime.id not in candidate.terminal_sessions:
            candidate.terminal_sessions.append(runtime.id)
        self.cancel_navigation()
        runtime.device_id = candidate.id
        candidate.active_terminal_id = runtime.id
        self.active_session, self.at_prompt = session, False
        if self.active_terminal_id == runtime.id:
            self.workspace.active_device_id = candidate.id
        self.persist_workspace()
        self.show_context()

    def set_status(self, text):
        if self._shutting_down:
            return False
        self.status.set_text(text)
        self.status.set_tooltip_text(text)
        return False

    def work(self, function, callback):
        if self._shutting_down:
            return None
        return self.file_work.submit(function, callback)

    def current_editor(self):
        if self._shutting_down:
            return None
        page = self.notebook.get_nth_page(self.notebook.get_current_page())
        return page if isinstance(page, EditorPane) else None

    def with_editor(self, callback):
        editor = self.current_editor()
        if editor:
            callback(editor)

    def update_keybinding_labels(self):
        if self._shutting_down: return
        for control, identity, title in self.keybinding_controls:
            binding = self.command_registry.binding(identity)
            hint = title + (' (' + binding + ')' if binding else '')
            if control.get_icon_name():
                control.set_tooltip_text(hint)
            else:
                control.set_label(hint)
        if hasattr(self, 'left_sidebar'): self.left_sidebar.update_tooltips()
        for editor in self.editors:
            if not editor._shutting_down:
                editor.editing.update_menu()
                for control,identity,hint in getattr(editor,'code_keybinding_controls',()):
                    binding = self.command_registry.binding(identity)
                    control.set_tooltip_text(' · '.join(value for value in [binding,hint] if value) or self.command_registry.commands[identity].title)

    def show_keybindings(self):
        if self._shutting_down: return
        from .keybinding_ui import KeybindingsView
        for existing in self.viewers:
            if isinstance(existing, KeybindingsView) and not existing._shutting_down:
                self.notebook.set_current_page(self.notebook.page_num(existing));return
        if len(self.viewers) >= 64:
            self.set_status('Close a view before opening Keybindings.');return
        view = KeybindingsView(self)
        self._add_viewer(view)

    def key_pressed(self, controller, key, keycode, state):
        if self._shutting_down: return False
        from .keybinding_host import event_shortcut, focused_context
        value = event_shortcut(key, state)
        context = focused_context(self)
        # GtkSourceView handles single-caret Tab at native tab stops. A remapped
        # indent command still works on the current line.
        if value == 'Tab' and context == 'editor':
            command = self.command_registry.resolve(value, context)
            if command and command.id == 'editor.indent' and not self.current_editor().buffer.get_has_selection(): return False
        handled = self.command_registry.dispatch(value, context) if value else False
        if not handled and context == 'editor':
            # These GTK defaults otherwise perform a second, hidden operation
            # after the user clears/remaps Naui's command (Ctrl+D deletes text,
            # Ctrl+H backspaces, Ctrl+F moves the caret, Ctrl+Z undoes).
            native = {'Ctrl+D':'editor.select_next','Ctrl+H':'editor.replace',
                      'Ctrl+F':'editor.find','Ctrl+Z':'editor.undo','Ctrl+Shift+Z':'editor.redo'}
            identity = native.get(value)
            if identity and self.command_registry.requested(identity) != value: return True
        return handled

    def on_message(self, message):
        if self._shutting_down:
            return
        action = message["action"]
        local = action not in {"open", "cwd", "navigation-ready", "busy", "session-ready"} or message.get("location") == "local"
        # The remote shell knows the OSC token but not this signing key. Only
        # local shell integration may register executable SSH transport options
        # or request a LOCAL file. Remote OSC requests are confined to an
        # already registered, live SSH session and its backend.
        if local and not verify_signature(message, self.terminal.local_key):
            self.set_status("Rejected unsigned local Naui request")
            return
        if action == "session-start":
            session = message["session"]
            try:
                self.register_remote(session, Connection(ssh_host(message["host"]), tuple(message["options"])))
            except ValueError as error:
                self.set_status(str(error))
                return
            self.set_status("SSH connecting: " + message["host"])
        elif action == "session-ready":
            session = message["session"]
            backend = self.sessions.get(session)
            if (backend and backend.connected and session in self.contexts
                    and self.session_is_current(session)):
                self.contexts[session]["state"] = "connected"
                device = self.workspace.devices[self.session_devices[session]]
                device.state = "connected"
                for viewer in self.viewers:
                    if viewer.device_id == device.id:
                        viewer.session = session
                        viewer.disconnected = False
                        viewer.update_label()
                        viewer.status.set_text("Reconnected to " + device.display_name + "; saves check the original revision.")
                        if isinstance(viewer, EditorPane) and viewer.revision is None and not viewer.dirty and not viewer.saving:
                            self.load_editor(viewer)
                        elif isinstance(viewer, EditorPane):
                            viewer.check_external()
                self.show_context()
                self.set_status("SSH connected: " + backend.connection.host)
        elif action == "session-end":
            if self.session_terminals.get(message["session"]) != self.runtime.id:
                return
            self.disconnect_session(message["session"])
            if self.active_session == message["session"]:
                self.active_session, self.at_prompt = None, False
                self.runtime.device_id = self.runtime.base_device_id
                if self.workspace.devices[self.runtime.base_device_id].type == "ssh":
                    device = self.workspace.devices[self.runtime.base_device_id]
                    self.contexts[None].update(path=device.navigation_state.current_path,
                                               home=device.navigation_state.home, state="disconnected")
                if self.active_terminal_id == self.runtime.id:
                    self.workspace.active_device_id = self.runtime.device_id
                    self.workspace.devices[self.runtime.device_id].active_terminal_id = self.runtime.id
                self.show_context()
        elif action in {"cwd", "navigation-ready", "busy"}:
            session = message.get("session") if message["location"] == "remote" else None
            context = self.contexts.get(session)
            if (session != self.active_session or context is None
                    or (session and (not context["backend"].connected or not self.session_is_current(session)))):
                self.set_status("Ignored shell event from an inactive or disconnected session")
                return
            if action == "busy":
                if self.navigation_pending and self.navigation_pending["phase"] == "prepare":
                    if self.navigation_pending.get('run'): self.pending_runs.pop(self.runtime.id, None)
                    self.cancel_navigation()
                    self.explorer.status.set_text("The shell became busy; retry navigation at the next prompt.")
                self.at_prompt = False
                self.explorer.update_controls()
                return
            pending = self.navigation_pending
            if pending and pending["phase"] == "prepare" and pending["session"] == session:
                if action != "navigation-ready":
                    return  # A cwd report alone does not acknowledge readline readiness.
                if message["path"] != pending["previous"]:
                    if pending.get('run'): self.pending_runs.pop(self.runtime.id, None)
                    self.cancel_navigation()
                    context.update(path=message["path"], home=message["home"])
                    self.at_prompt = True
                    self.show_context()
                    self.explorer.status.set_text("Shell directory changed; retry navigation.")
                    return
                # The session-only readline binding cleared the edit line and
                # acknowledged readiness. No cd is sent to arbitrary programs.
                pending["phase"] = "command"
                if pending.get('run'):
                    request = self.pending_runs.pop(self.runtime.id, None)
                    if request is not pending['run']:
                        self.cancel_navigation()
                        return
                    self.running_runs[request['id']] = self.runtime.id, request['device_id']
                    self.terminal.send_input(request['text'].encode())
                else:
                    self.terminal.send_input(cd_command(pending["target"], pending["ls"]).encode())
                self.at_prompt = False
                return
            self.at_prompt = True
            if hasattr(self, 'running_runs'):
                self.running_runs = {key: value for key, value in self.running_runs.items() if value[0] != self.runtime.id}
            context.update(path=message["path"], home=message["home"])
            if pending:
                self.navigation_pending = None
                self.sources.remove(self.navigation_timeout)
                self.navigation_timeout = None
            self.show_context()
            if pending:
                self.cancel_navigation()  # Clear histories not containing the resulting physical cwd.
            if pending and message["path"] == pending["previous"] and pending["target"] != pending["previous"]:
                self.explorer.status.set_text("Directory unchanged. Check the terminal for the cd error.")
            resume, self.runtime.resume_path = self.runtime.resume_path, None
            if session and resume and resume != message["path"]:
                # Restore this device's location through the SAME readiness
                # handshake, without stealing focus from another device/editor.
                self.navigate_terminal(resume, session, focus=False)
        elif action == "unsupported":
            if self.navigation_pending:
                self.cancel_navigation()
                self.explorer.status.set_text(message["reason"])
                self.explorer.update_controls()
            self.set_status(message["reason"])
        elif action == "open":
            if message.get("elevated"):
                self.set_status("Elevated editing is not supported. Use sudo /usr/bin/nano.")
                return
            session = message.get("session") if message["location"] == "remote" else None
            backend = self.sessions.get(session) if session else self.local_backend
            if (backend is None or (session and (not backend.connected or not self.session_is_current(session)
                                                 or self.session_terminals.get(session) != self.runtime.id))):
                self.set_status("Rejected request from an unknown or disconnected SSH session")
                return
            for filename in message["args"]:
                path = resolve_path(message["cwd"], filename, remote=bool(session))
                self.open_file(backend, path, session)

    def show_context(self):
        if self._shutting_down:
            return
        context = self.contexts[self.active_session]
        device = self.workspace.devices[self.runtime.device_id]
        if device.active_terminal_id not in {None, self.runtime.id}:
            self.update_workspace_ui()
            return  # A device tree follows its selected terminal, not other shells.
        device.active_terminal_id = self.runtime.id
        if context["path"]:
            device.observe(context["path"], context["home"])
            if device.type == "ssh" and not device.roots and device.state == "connected":
                device.add_root(context["path"])
                self.persist_workspace()
        sidebar = self.workspace_sidebar.explorers[device.id]
        sidebar.set_context(device.filesystem_backend, device.session_id,
                            device.navigation_state.current_path, device.navigation_state.home, device.state)
        self.update_workspace_ui()

        if hasattr(self, 'pending_runs'):
            self.prepare_run()

    def cancel_navigation(self):
        self.sources.remove(self.navigation_timeout)
        self.navigation_timeout = None
        self.navigation_pending = None
        for history in self.explorer.histories.values():
            history.cancel()

    def navigate_terminal(self, text, session, run_ls=False, *, focus=True):
        if self._shutting_down:
            return False
        if self.runtime is None or self.terminal is None:
            self.set_status('Start a terminal before navigating its directory.')
            return False
        context = self.contexts[self.active_session]
        if (session != self.active_session or not self.at_prompt or self.navigation_pending
                or self.terminal.closed or (session and not context["backend"].connected)):
            self.explorer.status.set_text("Wait for the active shell prompt before navigating.")
            return False
        try:
            target = navigation_path(text, context["path"], context["home"])
        except ValueError as error:
            self.explorer.status.set_text(str(error))
            return False
        self.navigation_pending = {"session": session, "target": target, "ls": run_ls,
                                   "phase": "prepare", "previous": context["path"]}
        self.at_prompt = False
        if focus:
            self.notebook.set_current_page(self.notebook.page_num(self.terminal))
            self.terminal.view.grab_focus()
        self.terminal.send_input(PREPARE_NAVIGATION)
        self.explorer.status.set_text("Navigating to " + target + "…")
        self.explorer.update_controls()
        pending = self.navigation_pending
        identity = self.runtime.id

        def timeout():
            self.navigation_timeout = None
            if not self._shutting_down and self.navigation_pending is pending:
                self.cancel_navigation()
                self.explorer.status.set_text("Shell did not acknowledge navigation. Press Enter in the terminal to resynchronize.")
                self.explorer.update_controls()
            return False
        self.navigation_timeout = self.sources.timeout(5000, self.in_terminal, identity, timeout)
        return True

    def check_mutation(self, backend, path, rename=False):
        for editor in self.editors:
            if editor.backend is backend and (editor.path == path or editor.path.startswith(path.rstrip("/") + "/")):
                raise ValueError("Close affected editor tabs before changing this path. Unsaved edits must be saved or explicitly discarded.")

    def confirm_action(self, title, description, button, callback):
        if self._shutting_down:
            return
        if self.dialog:
            self.dialog.present()
            return
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.WARNING,
                                   text=title, secondary_text=description)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(button, Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        self.dialog = dialog

        def response(dialog, answer):
            self.dialog = None
            self.signals.disconnect(dialog)
            dialog.destroy()
            if not self._shutting_down and answer == Gtk.ResponseType.ACCEPT:
                callback()
        self.signals.connect(dialog, "response", response)
        dialog.present()

    def open_file(self, backend, path, session=None, *, preview=True):
        """Explicit text editing: nano and Open in Editor never launch a player."""
        self.file_request = None
        if self._shutting_down:
            return
        for editor in self.editors:
            if editor.backend is backend and editor.path == path:
                if not preview: self.notebook.keep_open(editor)
                self.notebook.set_current_page(self.notebook.page_num(editor))
                return editor
        if len(self.viewers) >= 64:
            self.set_status("Close a file tab before opening more files (64-tab limit).")
            return
        editor = EditorPane(self, backend, path, session)
        self.editors.append(editor)
        self._add_viewer(editor, preview=preview)
        self.load_editor(editor)
        return editor

    def open_snapshot(self, backend, snapshot, session=None):
        """Open a navigation/rename snapshot without rebinding an existing tab."""
        if self._shutting_down:
            return None
        for editor in self.editors:
            if editor.backend is backend and editor.path == snapshot.path:
                self.notebook.set_current_page(self.notebook.page_num(editor))
                return editor
        if len(self.viewers) >= 64:
            self.set_status("Close a file tab before opening more files (64-tab limit).")
            return None
        editor = EditorPane(self, backend, snapshot.path, session)
        self.editors.append(editor)
        self._add_viewer(editor, preview=False)
        editor.loaded(snapshot)
        return editor

    def load_editor(self, editor):
        backend, path, session = editor.backend, editor.path, editor.session
        generation = getattr(backend, "generation", 0)
        editor.loading = True
        editor.update_label()

        def loaded(snapshot, error):
            if self._shutting_down or editor._shutting_down or editor not in self.editors:
                return
            if generation != getattr(backend, "generation", 0):
                return
            if error:
                editor.failed(error)
            else:
                editor.loaded(snapshot)
                if session and not backend.connected:
                    editor.mark_disconnected()
        self.work(lambda: backend.read(path), loaded)

    @staticmethod
    def preview_protected(viewer):
        return (getattr(viewer, 'dirty', False) or getattr(viewer, 'saving', False) or
                getattr(viewer, 'busy', False) or bool(getattr(viewer, 'dialogs', ())) or
                getattr(getattr(viewer, 'external', None), 'snapshot', None) is not None)

    def preview_viewer(self, viewer):
        tabs = self.notebook
        group = tabs.owner(viewer)
        for old in list(tabs.previews):
            if old is viewer or tabs.owner(old) is not group or old in tabs.pending_preview_closes: continue
            if old in tabs.pinned or self.preview_protected(old):
                tabs.keep_open(old)
            else:
                tabs.pending_preview_closes.add(old)
                # A view may open another view from its own GTK button/row signal.
                # Unparent it only after that native signal stack has returned.
                self.sources.timeout(0, self.replace_preview, old, group)
        tabs.preview(viewer)

    def replace_preview(self, old, group):
        tabs = self.notebook
        if old not in tabs.pending_preview_closes: return
        tabs.pending_preview_closes.discard(old)
        if self._shutting_down or old not in self.viewers or old not in tabs.previews: return
        if tabs.owner(old) is not group or old in tabs.pinned or self.preview_protected(old):
            tabs.keep_open(old)
        else:
            self.close_viewer(old)

    def _add_viewer(self, viewer, *, preview=True):
        self.viewers.append(viewer)
        tab = Gtk.Box(spacing=6)
        tab.append(viewer.label)
        if not hasattr(self, 'tab_menu'):
            from .tab_menu import TabMenu
            self.tab_menu = TabMenu(self)
        self.tab_menu.attach(tab, viewer, viewer.signals)
        gesture = Gtk.GestureClick(button=1)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        viewer.signals.connect(gesture, 'pressed', lambda _, count, *args:
            self.notebook.keep_open(viewer) if count == 2 else None)
        tab.add_controller(gesture)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.set_has_frame(False)
        viewer.tab_close_button = close
        viewer.signals.connect(close, "clicked", lambda *_: self.close_viewer(viewer))
        tab.append(close)
        index = self.notebook.append_page(viewer, tab)
        self.notebook.set_current_page(index)
        if preview: self.preview_viewer(viewer)

    def open_path(self, backend, path, session=None, *, preview=True):
        """Choose a viewer after a bounded binary probe; no GTK in the worker."""
        if self._shutting_down:
            return
        # A newer file click supersedes an older asynchronous probe.
        key = (backend, path)
        self.file_request = (key, preview, self.notebook.active)
        for viewer in self.viewers:
            if viewer.backend is backend and viewer.path == path:
                if not preview: self.notebook.keep_open(viewer)
                self.notebook.set_current_page(self.notebook.page_num(viewer))
                return
        key = (backend, path)
        if key in self.pending_paths:
            return
        if len(self.viewers) + len(self.pending_paths) >= 64:
            self.set_status("Close a file tab before opening more files (64-tab limit).")
            return
        self.pending_paths.add(key)
        self.set_status("Inspecting " + path + "…")

        def inspected(probe, error):
            self.pending_paths.discard(key)
            if self._shutting_down:
                return
            request = getattr(self, 'file_request', None)
            if request is None or request[0] != key: return
            if error:
                self.set_status(str(error))
                if isinstance(error, DisconnectedError):
                    self.disconnect_session(session, str(error))
                return
            request = getattr(self, 'file_request', None)
            if request is None or request[0] != key: return
            preview, group = request[1:]
            if group not in self.notebook.groups: return
            self.notebook.active = group
            spec = self.viewer_registry.select(path, probe.sample)
            # An explicit nano request may have opened the editor meanwhile.
            if any(viewer.backend is backend and viewer.path == path for viewer in self.viewers):
                self.open_path(backend, path, session, preview=preview)
                return
            if len(self.viewers) >= 64:
                # An explicit nano request can consume a slot while this
                # asynchronous MIME probe is still in flight.
                self.set_status("Close a file tab before opening more files (64-tab limit).")
                return
            if spec.kind == "text":
                self.open_file(backend, path, session, preview=preview)
            else:
                viewer = self.viewer_factories[spec.kind](self, backend, path, session)
                self._add_viewer(viewer, preview=preview)
                viewer.start()
        self.work(lambda: backend.probe(path), inspected)

    def close_viewer(self, viewer):
        if self._shutting_down or viewer._shutting_down:
            return
        if isinstance(viewer, EditorPane):
            self.close_editor(viewer)
            return
        if hasattr(self, 'tab_menu') and self.tab_menu.page is viewer: self.tab_menu.dismiss()
        viewer.close()
        self.notebook.remove_page(self.notebook.page_num(viewer))
        self.viewers.remove(viewer)

    def show_about(self):
        if self._shutting_down:
            return
        if self.dialog:
            self.dialog.present()
            return
        dialog = Gtk.AboutDialog(transient_for=self, modal=True, program_name=NAME,
                                 version=__version__, comments=DESCRIPTION,
                                 license_type=Gtk.License.MIT_X11,
                                 copyright="© 2026 Naui contributors", logo_icon_name="naui")
        url = project_url()
        if url:
            dialog.set_website(url)
            dialog.set_website_label("Project website")
        else:
            dialog.set_comments(DESCRIPTION + "\nProject/GitHub URL: not configured")
        self.dialog = dialog
        def closed(dialog, *_):
            self.dialog = None
            self.signals.disconnect(dialog)
            return False  # Let GTK close it normally.
        self.signals.connect(dialog, "close-request", closed)
        dialog.present()

    def disconnect_session(self, session, reason=None):
        if self._shutting_down or not self.session_is_current(session):
            return
        device = self.workspace.devices[self.session_devices[session]]
        runtime = self.runtimes[self.session_terminals[session]]
        runtime.contexts[session]['state'] = 'disconnected'
        runtime.at_prompt = False
        self.sources.remove(runtime.navigation_timeout)
        runtime.navigation_timeout = runtime.navigation_pending = None
        remaining = device.filesystem_backend.release(session)
        if remaining:
            survivors = [key for key, identity in self.session_devices.items()
                         if identity == device.id and self.session_is_current(key)]
            ready = [key for key in survivors if self.runtimes[self.session_terminals[key]].contexts[key]['state'] == 'connected']
            device.session_id = (ready or survivors)[-1] if survivors else None
            device.active_terminal_id = self.session_terminals[device.session_id] if survivors else None
            device.state = 'connected' if ready or not survivors else 'connecting'
            for viewer in self.viewers:
                if viewer.device_id == device.id:
                    viewer.session = device.session_id
                    if survivors and not ready: viewer.mark_disconnected('Waiting for the remaining SSH session to authenticate; contents preserved.')
        else:
            device.session_id = None
            device.state = 'disconnected'
            if hasattr(self, 'pending_runs'):
                self.pending_runs = {key: value for key, value in self.pending_runs.items() if value['device_id'] != device.id}
            if hasattr(self, 'transfers'):
                self.transfers.cancel_device(device.id)
            for viewer in self.viewers:
                if viewer.device_id == device.id:
                    viewer.mark_disconnected(reason) if reason else viewer.mark_disconnected()
        self.set_status('SSH session ended: ' + device.hostname)
        sidebar = self.workspace_sidebar.explorers[device.id]
        sidebar.set_context(device.filesystem_backend, device.session_id, device.navigation_state.current_path,
                            device.navigation_state.home, device.state)
        self.update_workspace_ui()

    def terminal_exited(self):
        if self._shutting_down:
            return
        for session in list(self.contexts):
            if session is not None:
                self.disconnect_session(session)
        self.at_prompt = False
        self.contexts[self.active_session]["state"] = "shell exited"
        device = self.workspace.devices[self.runtime.device_id]
        if device.active_terminal_id not in {None, self.runtime.id}:
            # A reconnect or another selected terminal now owns this device.
            # Drop only the exited terminal's timer: shared device history and
            # the replacement backend must survive its delayed process exit.
            self.sources.remove(self.navigation_timeout)
            self.navigation_timeout = self.navigation_pending = None
            self.update_workspace_ui()
            return
        self.cancel_navigation()
        if device.type == "ssh":
            if not device.filesystem_backend.connected:
                device.state = "disconnected"
        else:
            device.state = "shell exited"
        self.show_context()
        self.set_status("Shell exited. Editor tabs remain available.")

    def confirm_discard(self, message, callback):
        if self._shutting_down:
            return
        if self.dialog:
            self.dialog.present()
            return
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                   text=message, secondary_text="Cancel to keep editing or save your changes first.")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Discard changes", Gtk.ResponseType.REJECT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        self.dialog = dialog

        def response(dialog, answer):
            self.dialog = None
            self.signals.disconnect(dialog)
            dialog.destroy()
            if not self._shutting_down and answer == Gtk.ResponseType.REJECT:
                callback()
        self.signals.connect(dialog, "response", response)
        dialog.present()

    def close_editor(self, editor):
        if self._shutting_down or editor._shutting_down:
            return
        if editor.saving:
            editor.status.set_text("Wait for the save to complete before closing this tab.")
            return
        def remove():
            if self._shutting_down or editor._shutting_down:
                return
            if hasattr(self, "development"): self.development.closed_editor(editor)
            if hasattr(self, 'tab_menu') and self.tab_menu.page is editor: self.tab_menu.dismiss()
            editor.close()
            self.notebook.remove_page(self.notebook.page_num(editor))
            self.editors.remove(editor)
            self.viewers.remove(editor)
        if editor.dirty:
            self.confirm_discard("Discard unsaved changes to " + Path(editor.path).name + "?", remove)
        else:
            remove()

    def close_requested(self, *_):
        if self._shutting_down:
            return False
        if any(editor.saving for editor in self.editors):
            self.set_status("Wait for pending saves before closing Naui.")
            return True
        dirty = sum(editor.dirty for editor in self.editors)
        if dirty and not getattr(self, '_keep_drafts_approved', False):
            def keep_drafts():
                try:
                    self.session_restore.save()
                    if not self.session_restore.enabled: raise ValueError('Session recovery is unavailable; save your files before closing.')
                except (OSError, ValueError) as error:
                    self.set_status(str(error)); return
                self._keep_drafts_approved = True
                self.close()
            self.confirm_action('Close and keep unsaved drafts?',
                                f'{dirty} editor draft(s) will be stored privately on this computer and restored next time. Files are not saved to their devices.',
                                'Keep Drafts and Close', keep_drafts)
            return True
        active = self.transfers.active() if hasattr(self, 'transfers') else 0
        if active and not getattr(self, '_transfers_exit_approved', False):
            def approve():
                self._transfers_exit_approved = True
                self.close()
            self.confirm_action('Transfers are still active',
                                f'{active} transfer(s) will be cancelled. Incomplete files will not replace destinations.',
                                'Exit and Cancel Transfers', approve)
            return True
        self.shutdown()
        return False  # GTK owns window destruction and application exit.

    def shutdown(self):
        if self._shutting_down:
            return
        if self.workspace_dirty:
            self.persist_workspace()
        if hasattr(self, 'ui_settings'):
            self.save_ui()
        if hasattr(self, "session_restore"):
            try: self.session_restore.save()
            except (OSError, ValueError) as error: self.set_status("Session recovery: " + str(error))
        self._shutting_down = True
        if hasattr(self, 'tab_menu'): self.tab_menu.close()
        self.alive = False
        if hasattr(self, 'right_panels'): self.right_panels.close()
        if hasattr(self, 'search_panel'): self.search_panel.close()
        manager_view = getattr(self, 'plugin_manager_view', None)
        if manager_view: manager_view.close()  # Release its dialogs before window dialog cleanup.
        if hasattr(self, 'ai_agents'): self.ai_agents.close()
        self.at_prompt = False
        self.navigation_pending = None
        for runtime in self.runtimes.values():
            runtime.closed, runtime.at_prompt = True, False
            runtime.navigation_pending = runtime.navigation_timeout = None
        self.sources.close()
        if hasattr(self, 'transfer_panel'):
            self.transfer_panel.close()
        if hasattr(self, 'plugins'):
            self.plugins.close()
        if hasattr(self, 'file_watch'):
            self.file_watch.close()
        if hasattr(self, 'command_service'):
            self.command_service.close()
        for section in getattr(self, 'sidebar_sections', {}).values(): section.close()
        if hasattr(self, 'transfers'):
            self.transfers.close()
        if hasattr(self, 'authentication'):
            self.authentication.close()
        if hasattr(self, "code_navigation"):
            self.code_navigation.close()
        self.navigation_timeout = None
        self.file_work.stop()
        self.preview_cache.cancel()
        backends = {id(backend): backend for backend in self.sessions.values()}
        backends.update({id(device.filesystem_backend): device.filesystem_backend
                         for device in self.workspace.devices.values() if device.type == "ssh"})
        for backend in backends.values():
            backend.close()
        self.signals.close()
        close_tabs = getattr(getattr(self, "notebook", None), "close", None)
        if close_tabs: close_tabs()
        if self.dialog:
            self.dialog.destroy()
            self.dialog = None
        for editor in self.viewers:
            editor.close()
        self.workspace_sidebar.close()
        if hasattr(self, 'left_sidebar'): self.left_sidebar.close()
        if hasattr(self, 'command_registry'): self.command_registry.close()
        for terminal in self.terminals.values():
            terminal.close()
        if hasattr(self, 'editor_css'):
            Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.editor_css)
        self.file_work.join()
        if hasattr(self, 'search_panel'): self.search_panel.service.join()
        if hasattr(self, 'file_watch'):
            self.file_watch.join()
        if hasattr(self, 'transfers'):
            self.transfers.join()
        if hasattr(self, "code_navigation"):
            self.code_navigation.cleanup()
        if hasattr(self, 'authentication') and hasattr(self.authentication, 'cleanup'):
            self.authentication.cleanup()
        self.preview_cache.close()
        for backend in backends.values():
            if hasattr(backend, 'cleanup_mounts'):
                backend.cleanup_mounts()
        self.editors.clear()
        self.viewers.clear()
        self.pending_paths.clear()

    def save_copy(self, editor):
        if self._shutting_down or editor._shutting_down:
            return
        if editor.copy_dialog:
            editor.copy_dialog.show()
            return
        chooser = Gtk.FileChooserNative(title="Save a local copy", transient_for=self, modal=True,
                                        action=Gtk.FileChooserAction.SAVE,
                                        accept_label="Save copy", cancel_label="Cancel")
        chooser.set_current_name(Path(editor.path).name)
        # Keep native dialogs alive until their asynchronous response.
        editor.copy_dialog = chooser

        def response(dialog, answer):
            editor.copy_dialog = None
            editor.signals.disconnect(dialog)
            if self._shutting_down or editor._shutting_down:
                dialog.destroy()
                return
            if answer != Gtk.ResponseType.ACCEPT:
                dialog.destroy()
                return
            chosen = dialog.get_file()
            path = chosen.get_path() if chosen else None
            dialog.destroy()
            if not path:
                return
            content = editor.text()
            backend = self.local_backend
            editor.saving = True

            def copy():
                # Save-copy deliberately requires a NEW file: existing files
                # are never overwritten as an incidental conflict workaround.
                snapshot = backend.read(path)
                if snapshot.revision != "missing":
                    raise ValueError("Choose a new filename for the local copy; destination exists.")
                return backend.write(path, content, "missing")

            def done(snapshot, error):
                if self._shutting_down or editor._shutting_down:
                    return
                editor.saving = False
                editor.status.set_text(str(error) if error else "Copy saved to " + path + ". Original tab remains unsaved.")
            self.work(copy, done)
        editor.signals.connect(chooser, "response", response)
        chooser.show()


class NauiApplication(Gtk.Application):
    def __init__(self, paths, cwd, plugin_paths=()):
        from gi.repository import Gio
        super().__init__(application_id="io.github.naui.Naui", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.paths, self.cwd = paths, cwd
        self.plugin_paths = tuple(plugin_paths)
        self.window = None
        self._shutting_down = False

    def do_activate(self):
        if self._shutting_down:
            return
        if self.window is None:
            self.window = NauiWindow(self, self.paths, self.cwd)
        self.window.present()

    def do_shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.window:
            self.window.shutdown()
        # Release the Python/native window graph while GTK is alive, on
        # the main thread, rather than during interpreter finalization.
        self.window = None
        Gtk.Application.do_shutdown(self)

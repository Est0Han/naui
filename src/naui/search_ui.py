"""Persistent Search activity, virtualized file groups and streamed plain results."""
import posixpath
from gi.repository import Gio, GLib, GObject, Gtk, Pango

from .lifecycle import Signals, Sources
from .search_model import DEFAULT_EXCLUDES, SearchQuery, SearchRoot, patterns
from .search_service import WorkspaceSearchService


class SearchNode(GObject.Object):
    count = GObject.Property(type=int, default=0)
    def __init__(self, result=None, title='', group=False):
        super().__init__()
        self.result, self.title, self.group = result, title, group
        self.children = Gio.ListStore.new(SearchNode) if group else None
        self.count = 0


def workspace_roots(workspace, scope='workspace', selected_folder=None):
    roots, seen = [], set()
    for device in workspace.devices.values():
        if scope != 'workspace' and device.id != workspace.active_device_id: continue
        paths = sorted((root.path for root in device.roots),key=lambda value:(len(value),value))
        if scope == 'root':
            current = device.navigation_state.current_path
            eligible = [path for path in paths if current and (current == path or current.startswith(path.rstrip('/') + '/'))]
            paths = [max(eligible, key=len)] if eligible else paths[:1]
        if scope == 'folder':
            paths = [selected_folder] if selected_folder and any(selected_folder == path or selected_folder.startswith(path.rstrip('/') + '/') for path in paths) else []
        for path in paths:
            if (device.id, path) in seen: continue
            # Nested pinned roots needn't be searched twice. Device identity is
            # retained even for identical logical paths on separate machines.
            if any(identity == device.id and path.startswith(parent.rstrip('/') + '/') for identity, parent in seen): continue
            seen.add((device.id, path))
            roots.append(SearchRoot(device.id, path, device.filesystem_backend, device.type == 'ssh', device.display_name))
    return tuple(roots)


class SearchPanel(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, vexpand=True)
        self.window, self.closed = window, False
        self.signals, self.sources = Signals(), Sources()
        self.service = WorkspaceSearchService()
        self.watch = window.file_watch.observe(lambda event:self.service.file_cache.invalidate()) if hasattr(window,'file_watch') else None
        self.session, self.debounce = None, None
        self.groups, self.results, self.count, self.file_ids = {}, [], 0, set()
        self.warning_text, self.progress = [], ''
        self.popovers = {}
        self.row_bindings, self.navigation_request = {}, 0
        self.pending_navigation, self.sorted_session = None, None
        header = Gtk.Box(spacing=2)
        header.append(Gtk.Label(label='SEARCH', xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        for icon, tooltip, callback in [('process-stop-symbolic','Cancel Search',self.cancel),('edit-clear-symbolic','Clear Search',self.clear)]:
            button = Gtk.Button.new_from_icon_name(icon); button.set_has_frame(False); button.set_tooltip_text(tooltip)
            button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
            self.signals.connect(button, 'clicked', lambda *_, callback=callback: callback()); header.append(button)
        self.append(header)
        self.mode = Gtk.ComboBoxText()
        self.mode.append('content', 'Contents'); self.mode.append('files', 'Files / Folders'); self.mode.set_active_id('content')
        self.mode.set_tooltip_text('Content Search or File / Folder Search')
        self.append(self.mode)
        self.entry = Gtk.Entry(placeholder_text='Search text…', hexpand=True, width_chars=1, max_length=4096)
        self.entry.set_tooltip_text('Search configured workspace roots. Enter searches immediately.')
        self.append(self.entry)
        self.replace_entry = Gtk.Entry(placeholder_text='Replace with…', width_chars=1, max_length=4096)
        replacement = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        replacement.append(self.replace_entry)
        preview_button = Gtk.Button(label='Preview Replacement…')
        replacement.append(preview_button)
        self.replace_expander = Gtk.Expander(label='Replace in matched files')
        self.replace_expander.set_child(replacement); self.append(self.replace_expander)
        def replace_preview(*_):
            from .replacement import preview
            preview(self)
        self.signals.connect(preview_button, 'clicked', replace_preview)
        options = Gtk.Box(spacing=2)
        self.regex, self.case, self.word = [Gtk.ToggleButton(label=label, tooltip_text=tooltip) for label,tooltip in
            [('.*','Regular Expression'),('Aa','Case Sensitive'),('Ab','Whole Word')]]
        for control in (self.regex,self.case,self.word): options.append(control)
        self.append(options)
        self.include = Gtk.Entry(placeholder_text='Files to include', width_chars=1, hexpand=True, max_length=4096)
        self.exclude = Gtk.Entry(placeholder_text='Files to exclude', width_chars=1, hexpand=True, max_length=4096)
        self.exclude.set_text(','.join(DEFAULT_EXCLUDES))
        for title, control in [('Files to include',self.include),('Files to exclude',self.exclude)]:
            control.set_tooltip_text(title + ': comma-separated globs, e.g. *.py,src/**; * matches one component, ** is recursive.')
            control.update_property([Gtk.AccessibleProperty.LABEL], [title]); self.append(control)
        self.hidden = Gtk.CheckButton(label='Include hidden files')
        self.ignored = Gtk.CheckButton(label='Include ignored files')
        self.append(self.hidden); self.append(self.ignored)
        self.scope = Gtk.ComboBoxText()
        for identity,title in [('workspace','Workspace'),('root','Current Root'),('device','Device'),('folder','Selected Folder')]: self.scope.append(identity,title)
        self.scope.set_active_id('workspace'); self.scope.set_tooltip_text('Only configured roots are searched; no whole-device scan.')
        self.append(self.scope)
        self.status = Gtk.Label(label='Enter a query to search the workspace.', xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, width_chars=1)
        self.append(self.status)
        self.store = Gio.ListStore.new(SearchNode)
        self.tree = Gtk.TreeListModel.new(self.store, False, False, lambda node: node.children)
        self.selection = Gtk.SingleSelection.new(self.tree)
        self.selection.set_autoselect(False)
        factory = Gtk.SignalListItemFactory()
        self.signals.connect(factory,'setup',self.setup_row); self.signals.connect(factory,'bind',self.bind_row)
        self.signals.connect(factory,'unbind',self.unbind_row)
        self.list = Gtk.ListView.new(self.selection,factory)
        self.list.set_single_click_activate(True)
        self.signals.connect(self.list,'activate',lambda _,position: self.open_result(position))
        self.scroll = Gtk.ScrolledWindow(hexpand=True,vexpand=True)
        self.scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_child(self.list); self.append(self.scroll)
        for control in (self.entry,self.include,self.exclude): self.signals.connect(control,'changed',self.changed)
        for control in (self.mode,self.scope): self.signals.connect(control,'changed',self.changed)
        for control in (self.regex,self.case,self.word,self.hidden,self.ignored): self.signals.connect(control,'toggled',self.changed)
        self.signals.connect(self.entry,'activate',lambda *_:self.search())
        self.sources.timeout(60,self.poll)

    def setup_row(self, _, item):
        expander = Gtk.TreeExpander(hexpand=True)
        box = Gtk.Box(spacing=4,hexpand=True)
        label = Gtk.Label(xalign=0,ellipsize=Pango.EllipsizeMode.END,hexpand=True)
        label.set_width_chars(1)
        count = Gtk.Label(); count.add_css_class('dim-label')
        box.append(label); box.append(count); expander.set_child(box); item.set_child(expander)
        item.search_label, item.search_count = label, count
        gesture = Gtk.GestureClick(button=3)
        self.signals.connect(gesture,'pressed',lambda gesture,*args:self.context_menu(item))
        expander.add_controller(gesture)

    def bind_row(self, _, item):
        row = item.get_item(); node = row.get_item(); result = node.result
        item.get_child().set_list_row(row)
        if result and result.line and not node.group:
            offset = max(0,result.match_start-80)
            first = min(len(result.preview),result.match_start-offset+(1 if offset else 0))
            last = min(len(result.preview),first+result.match_end-result.match_start)
            snippet = (GLib.markup_escape_text(result.preview[:first]) + '<b>' + GLib.markup_escape_text(result.preview[first:last]) +
                       '</b>' + GLib.markup_escape_text(result.preview[last:]))
            item.search_label.set_markup(str(result.line) + '  ' + snippet)
        else: item.search_label.set_text(node.title)
        item.search_count.set_text(str(node.count) if node.group else '')
        item.count_owner = node if node.group else None
        item.count_handler = node.connect('notify::count',lambda *_:item.search_count.set_text(str(node.count))) if node.group else None
        if node.group: self.row_bindings[item] = node,item.count_handler
        item.get_child().set_tooltip_text((result.device_id + ':' + result.logical_path + (':' + str(result.line) if result.line else '')) if result else node.title)

    def unbind_row(self, _, item):
        for popover in list(self.popovers):
            if popover.get_parent() is item.get_child(): self.close_popover(popover)
        if item.count_owner and item.count_handler:
            item.count_owner.disconnect(item.count_handler)
        item.count_owner, item.count_handler = None, None
        self.row_bindings.pop(item,None)
        item.get_child().set_list_row(None)

    def changed(self, *_):
        if self.closed: return
        self.service.cancel()  # obsolete results never arrive while debounce waits
        self.session = None
        self.sources.remove(self.debounce)
        self.debounce = self.sources.timeout(220,self.search)
        self.regex.set_sensitive(self.mode.get_active_id() == 'content')
        self.word.set_sensitive(self.mode.get_active_id() == 'content')

    def query(self):
        selected = None
        explorer = self.window.explorer
        if self.scope.get_active_id() == 'folder':
            selected = explorer.selected_path()
            row = explorer.selection.get_selected_item()
            entry = row.get_item().entry if row else None
            if entry and selected and not entry.is_directory: selected = posixpath.dirname(selected)
        roots = workspace_roots(self.window.workspace,self.scope.get_active_id(),selected)
        return SearchQuery(self.entry.get_text(),self.mode.get_active_id(),self.case.get_active(),self.word.get_active(),self.regex.get_active(),
            patterns(self.include.get_text()),patterns(self.exclude.get_text()),self.hidden.get_active(),self.ignored.get_active(),roots=roots)

    def reset_results(self):
        for popover in list(self.popovers): self.close_popover(popover)
        self.store.remove_all(); self.groups.clear(); self.results.clear(); self.file_ids.clear(); self.count = 0
        self.warning_text.clear()
        self.sorted_session = None

    def search(self):
        if self.closed: return
        self.sources.remove(self.debounce); self.debounce = None
        self.service.cancel(); self.session = None
        if not self.entry.get_text(): self.reset_results(); self.status.set_text('Enter a query to search the workspace.'); return
        try: query = self.query()
        except ValueError as error: self.status.set_text(str(error)); return
        self.reset_results()
        if not query.roots: self.status.set_text('No workspace roots in this scope. Add a root in Explorer.'); return
        self.session = self.service.start(query)
        self.status.set_text('Searching ' + str(len(query.roots)) + ' workspace roots…')

    def add_result(self, result):
        self.count += 1; self.file_ids.add(result.identity); self.results.append(result)
        title = result.relative_path
        if result.line:
            if result.identity not in self.groups:
                group = SearchNode(result, result.device_id + ' · ' + posixpath.basename(result.workspace_root) + ' / ' + title, True)
                self.groups[result.identity] = group; self.store.append(group)
                # Expand newly discovered files; switching panels doesn't touch rows.
                row = self.tree.get_row(self.tree.get_n_items()-1)
                if row and row.get_item() is group: row.set_expanded(True)
            group = self.groups[result.identity]
            group.count += 1; group.children.append(SearchNode(result, str(result.line) + '  ' + result.preview))
        else:
            node = SearchNode(result, result.device_id + ' · ' + title + ('/' if result.directory else ''))
            self.store.append(node)

    def poll(self):
        if self.closed: return
        pending = self.pending_navigation
        if pending:
            request,editor,result = pending
            if request != self.navigation_request or editor not in self.window.editors or editor._shutting_down:
                self.pending_navigation = None
            elif not editor.loading:
                self.pending_navigation = None
                self.window.notebook.set_current_page(self.window.notebook.page_num(editor))
                self.place_match(editor,result)
        session = self.session
        if session:
            for kind, value in session.drain():
                if kind == 'result': self.add_result(value)
                elif kind == 'warning': self.warning_text.append(value)
                elif kind == 'progress': self.progress = value
            running = session.state in {'pending','running'} or not session.events.empty()
            if not running:
                self.warning_text = list(dict.fromkeys(self.warning_text + session.warnings))[:12]
            summary = (str(self.count) + ' files / folders' if session.query.mode == 'files' else
                       str(self.count) + ' results in ' + str(len(self.file_ids)) + ' files')
            if running: summary += ' · Searching ' + self.progress + '…'
            elif session.state == 'cancelled': summary += ' · Cancelled / incomplete'
            elif session.state == 'failed': summary += ' · Failed'
            if session.limited: summary += '\nSearch limit reached. Refine your query or scope.'
            if self.warning_text: summary += '\n' + self.warning_text[0]
            self.status.set_text(summary)
            self.status.set_tooltip_text('\n'.join(self.warning_text) or summary)
            if not running and session.query.mode == 'files' and self.results and self.sorted_session != session.id:
                self.sorted_session = session.id
                # Sort once at completion; content search remains streamed/grouped.
                ordered = sorted(self.results,key=lambda result:(-result.score,result.device_id,result.relative_path))
                if ordered != self.results:
                    self.results[:] = ordered
                    self.store.remove_all()
                    for result in ordered: self.store.append(SearchNode(result,result.device_id + ' · ' + result.relative_path + ('/' if result.directory else '')))
        self.sources.timeout(60,self.poll)

    def focus_search(self, mode=None):
        self.window.show_sidebar_panel('search')
        if mode: self.mode.set_active_id(mode)
        self.entry.grab_focus()

    def cancel(self):
        self.sources.remove(self.debounce); self.debounce = None; self.service.cancel()

    def clear(self):
        self.cancel(); self.session = None; self.entry.set_text(''); self.reset_results(); self.status.set_text('Search cleared.')

    def move_result(self, direction):
        total = self.tree.get_n_items()
        if not total: return
        current = self.selection.get_selected()
        target = 0 if current >= total else max(0,min(total-1,current+direction))
        self.selection.set_selected(target); self.list.grab_focus()

    def reveal(self, result):
        explorer = self.window.workspace_sidebar.explorers.get(result.device_id)
        if not explorer: return
        self.window.show_sidebar_panel('explorer')
        self.window.workspace_sidebar.cards[result.device_id][0].set_expanded(True)
        explorer.pending_select = result.logical_path
        explorer.set_context(explorer.backend,explorer.session,result.logical_path if result.directory else posixpath.dirname(result.logical_path),explorer.home,explorer.connection_state)
        explorer._restore_state()

    def open_result(self, position=None):
        if position is None: position = self.selection.get_selected()
        row = self.tree.get_row(position)
        if not row: return
        node = row.get_item(); result = node.result
        if node.group: row.set_expanded(not row.get_expanded()); return
        self.open_value(result)

    def open_value(self, result):
        if self.closed: return
        self.navigation_request += 1
        request = self.navigation_request
        if result.directory: self.reveal(result); return
        device = self.window.workspace.devices.get(result.device_id)
        if not device: self.window.set_status('Search device is no longer in the workspace.'); return
        if not device.connected: self.window.set_status('Search device disconnected; reconnect before opening this result.'); return
        if not result.line: self.window.open_path(device.filesystem_backend,result.logical_path,device.session_id); return
        # Normal snapshot pipeline deduplicates editors and preserves unsaved text.
        generation = getattr(device.filesystem_backend,'generation',0)
        def ready(snapshot,error):
            if self.closed or self.window._shutting_down or request != self.navigation_request: return
            if generation != getattr(device.filesystem_backend,'generation',0):
                self.window.set_status('Device connection changed during navigation; retry.'); return
            if error: self.window.set_status(str(error)); return
            editor = self.window.open_snapshot(device.filesystem_backend,snapshot,device.session_id)
            if editor: self.place_match(editor,result)
        existing = next((editor for editor in self.window.editors if editor.device_id == result.device_id and editor.path == result.logical_path),None)
        if existing:
            self.window.notebook.set_current_page(self.window.notebook.page_num(existing))
            if existing.loading:
                self.pending_navigation = request,existing,result
            else: self.place_match(existing,result)
        else: self.window.work(lambda:device.filesystem_backend.read(result.logical_path),ready)

    @staticmethod
    def place_match(editor,result):
        buffer = editor.buffer
        line = min(max(0,result.line-1),buffer.get_line_count()-1)
        first = buffer.get_iter_at_line(line)
        if isinstance(first,tuple): first = first[-1]
        end = first.copy(); end.forward_to_line_end()
        length = end.get_line_offset()
        first.set_line_offset(min(result.match_start,length))
        end.set_line_offset(min(result.match_end,length))
        buffer.select_range(end,first)
        editor.view.scroll_to_iter(first,0.2,True,0,0.5); editor.view.grab_focus()

    def context_menu(self,item):
        row = item.get_item()
        if not row: return
        result = row.get_item().result
        if not result: return
        popover = Gtk.Popover(); box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        signals = Signals(); self.popovers[popover] = signals
        actions = [('Open',lambda:self.open_value(result)),('Reveal in Explorer',lambda:self.reveal(result)),
                   ('Copy Path',lambda:self.get_clipboard().set(result.device_id + ':' + result.logical_path)),
                   ('Copy Relative Path',lambda:self.get_clipboard().set(result.relative_path))]
        for title,callback in actions:
            button = Gtk.Button(label=title)
            def clicked(*_,callback=callback):
                popover.popdown(); self.sources.idle(callback)
            signals.connect(button,'clicked',clicked); box.append(button)
        popover.set_child(box); popover.set_parent(item.get_child())
        signals.connect(popover,'closed',lambda *_:self.sources.idle(self.close_popover,popover))
        popover.popup()

    def close_popover(self,popover):
        signals = self.popovers.pop(popover,None)
        if signals:
            signals.close()
            if popover.get_parent(): popover.unparent()

    def workspace_changed(self):
        self.service.file_cache.invalidate()
        if self.session:
            roots = {(root.device_id,root.path) for root in workspace_roots(self.window.workspace)}
            if any((root.device_id,root.path) not in roots or not getattr(root.backend,'connected',True) for root in self.session.query.roots): self.cancel()

    def close(self):
        if self.closed: return
        self.closed = True; self.cancel(); self.sources.close(); self.signals.close(); self.service.close()
        if self.watch: self.watch.close()
        for popover in list(self.popovers): self.close_popover(popover)
        for node,handler in self.row_bindings.values():
            if node.handler_is_connected(handler): node.disconnect(handler)
        self.row_bindings.clear()
        self.pending_navigation = None

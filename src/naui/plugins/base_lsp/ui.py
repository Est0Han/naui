"""GTK adapter for device-owned code navigation. Servers never own GTK objects."""
from dataclasses import dataclass, field
import difflib
import keyword
import posixpath

from gi.repository import Gdk, Gtk, GtkSource, Pango, Vte

from ...code_navigation import (CodeHistory, CodeLocation, TERMINAL_PATH_PATTERN, TextPositions, offset_at,
                              path_links, position_at, validate_position)
from ...lifecycle import FileWork, Signals, Sources
from .workspace import LspSession, project_root


@dataclass
class Document:
    editor: object
    version: int = 0
    session: object = None
    change_source: int | None = None
    hover_source: int | None = None
    hover_offset: int | None = None
    links: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    result_signals: Signals = field(default_factory=Signals)
    pending_location: object = None


class CodeNavigation:
    def __init__(self, window):
        self.window = window
        self.closed = False
        self.sources = Sources()
        # Slow language requests/indexing must not occupy the filesystem/save
        # worker pool. This manager keeps GTK completion owners on the UI thread.
        self.file_work = FileWork(self.sources)
        self.documents, self.sessions, self.terminal_signals = {}, {}, {}
        self.history = CodeHistory()
        self.history_pending = False
        self.poll_source = None
        self._version = 0
        features = window.plugins.features if hasattr(window, 'plugins') else {}
        self.path_parser = features.get('path_parser', path_links)
        self.path_opener = features.get('path_open', lambda location: self.open_link(location))
        self.terminal_path_pattern = features.get('terminal_path_pattern', TERMINAL_PATH_PATTERN)
        self.language_factories = features.get('languages', {'.py': LspSession, '.pyi': LspSession})

    def next_version(self):
        # Monotonic across tab close/reopen, so late work from an old tab can
        # neither replace nor close the new LSP document with the same path.
        self._version += 1
        return self._version

    def work(self, job, done):
        return None if self.closed else self.file_work.submit(job, done)

    def build_editor(self, editor):
        document = self.documents[editor] = Document(editor)
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                          min_children_per_line=1, max_children_per_line=8,
                          column_spacing=3, row_spacing=0)
        actions = [("Definition", "F12 / Ctrl+Click", lambda: self.query(editor, "definition")),
                   ("References", "Shift+F12", lambda: self.query(editor, "references")),
                   ("Hover", "Ctrl+K or pause over a symbol", lambda: self.query(editor, "hover")),
                   ("Rename", "F2 — preview buffer edits", lambda: self.rename_dialog(editor)),
                   ("Problems", "Diagnostics for this document", lambda: self.show_problems(editor)),
                   ("← Code", "Alt+Left", lambda: self.history_move(-1)),
                   ("Code →", "Alt+Right", lambda: self.history_move(1)),
                   ("Reindex", "Refresh the remote source mirror", lambda: self.reindex(editor))]
        editor.code_keybinding_controls = []
        for name, hint, action in actions:
            button = Gtk.Button(label=name)
            button.set_has_frame(False)
            identity = {'Definition':'navigation.definition','References':'navigation.references',
                        'Hover':'navigation.hover','Rename':'navigation.rename','← Code':'navigation.back','Code →':'navigation.forward'}.get(name)
            if identity:
                hint = {'Definition':'Ctrl+Click also navigates','Hover':'Or pause over a symbol','Rename':'Preview buffer edits'}.get(name,'')
                editor.code_keybinding_controls.append((button,identity,hint))
                registry = getattr(editor.window,'command_registry',None)
                binding = registry.binding(identity) if registry and identity in registry.commands else None
                button.set_tooltip_text(' · '.join(value for value in [binding,hint] if value) or name)
            else: button.set_tooltip_text(hint)
            editor.signals.connect(button, "clicked", lambda *_, action=action: action())
            row.append(button)
        editor.prepend(row)
        editor.code_status = Gtk.Label(label="Code navigation: loading…", xalign=0, wrap=True, selectable=True)
        editor.append(editor.code_status)
        editor.code_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        editor.code_results_scroll = Gtk.ScrolledWindow()
        editor.code_results_scroll.set_max_content_height(200)
        editor.code_results_scroll.set_propagate_natural_height(True)
        editor.code_results_scroll.set_child(editor.code_results)
        editor.code_results_scroll.set_visible(False)
        editor.append(editor.code_results_scroll)
        editor.code_link_tag = editor.buffer.create_tag("naui-path-link", underline=Pango.Underline.SINGLE)
        editor.code_error_tag = editor.buffer.create_tag("naui-code-error", underline=Pango.Underline.ERROR)
        editor.code_warning_tag = editor.buffer.create_tag("naui-code-warning", underline=Pango.Underline.SINGLE)
        editor.view.set_show_line_marks(True)
        for category, icon, priority in [('naui-error','dialog-error-symbolic',10),('naui-warning','dialog-warning-symbolic',9)]:
            attributes = GtkSource.MarkAttributes()
            attributes.set_icon_name(icon)
            editor.view.set_mark_attributes(category,attributes,priority)
        editor.view.set_has_tooltip(True)
        editor.signals.connect(editor.view,'query-tooltip',lambda view,x,y,keyboard,tooltip: self.diagnostic_tooltip(editor,x,y,keyboard,tooltip))
        editor.signals.connect(editor.buffer, "changed", lambda *_: self.changed(editor))
        click = Gtk.GestureClick()
        click.set_button(1)
        editor.signals.connect(click, "pressed", lambda gesture, count, x, y: self.clicked(editor, gesture, x, y))
        editor.view.add_controller(click)
        motion = Gtk.EventControllerMotion()
        editor.signals.connect(motion, "motion", lambda _, x, y: self.motion(editor, x, y))
        editor.signals.connect(motion, "leave", lambda *_: self.leave(editor))
        editor.view.add_controller(motion)
        if self.poll_source is None:
            self.poll_source = self.sources.timeout(200, self.poll)

    def loaded(self, editor):
        if self.closed or editor not in self.documents:
            return
        document = self.documents[editor]
        document.version = self.next_version()
        if getattr(editor,'document',None): editor.document.lsp_version = document.version
        if getattr(getattr(editor, 'document', None), 'large', False):
            session, document.session = document.session, None
            if session:
                path, version = editor.path, document.version
                self.work(lambda: session.detach(path,version),lambda *_: None)
            editor.code_status.set_text('Large file mode: code analysis and automatic path highlighting disabled.')
            if document.pending_location:
                location, document.pending_location = document.pending_location, None
                self.place(editor, location)
            return
        device = self.window.workspace.devices[editor.device_id]
        provider = next((factory for suffix, factory in self.language_factories.items() if editor.path.endswith(suffix)), None)
        if provider:
            root = project_root(editor.path, [root.path for root in device.roots], remote=device.type == "ssh")
            key = (device.id, root)
            if key not in self.sessions and len(self.sessions) >= 8:
                editor.code_status.set_text("Code analysis is limited to 8 projects per run.")
            else:
                if key not in self.sessions:
                    self.sessions[key] = provider(device.id, editor.backend, root, remote=device.type == "ssh")
                document.session = self.sessions[key]
                self.sync(editor)
        else:
            editor.code_status.set_text("Ctrl+Click a file path. Pyright supports Python files (.py/.pyi).")
        self.update_links(editor)
        if document.pending_location:
            location, document.pending_location = document.pending_location, None
            self.place(editor, location)

    def changed(self, editor):
        if self.closed or editor.loading or editor not in self.documents:
            return
        document = self.documents[editor]
        document.version = self.next_version()
        if getattr(editor,'document',None): editor.document.lsp_version = document.version
        self.clear_diagnostics(editor)
        self.sources.remove(document.change_source)
        if getattr(getattr(editor, 'document', None), 'large', False):
            document.change_source = None
            session, document.session = document.session, None
            if session:
                path, version = editor.path, document.version
                self.work(lambda: session.detach(path, version), lambda *_: None)
            self.update_links(editor)
            editor.code_status.set_text('Large file mode: code analysis disabled.')
            return
        document.change_source = self.sources.timeout(350, self.sync, editor)

    def sync(self, editor):
        if self.closed or editor._shutting_down or editor.loading or editor not in self.documents:
            return
        document = self.documents[editor]
        document.change_source = None
        self.update_links(editor)
        if not document.session:
            return
        session, path, text, version = document.session, editor.path, editor.text(), document.version
        editor.code_status.set_text("Pyright: analyzing…")
        def done(warning, error):
            if not self.valid(editor, version):
                return
            editor.code_status.set_text(str(error) if error else "Pyright ready" + (" — " + warning if warning else ""))
        self.work(lambda: session.sync(path, text, version), done)

    def saved(self, editor, saved_text=None):
        document = self.documents.get(editor)
        if self.closed or not document or not document.session:
            return
        session, path, text, version = document.session, editor.path, editor.text(), document.version
        self.work(lambda: session.saved(path, text, version, saved_text),
                         lambda _, error: editor.code_status.set_text(str(error)) if error and self.valid(editor, version) else None)

    def valid(self, editor, version=None):
        return (not self.closed and not self.window._shutting_down and not editor._shutting_down
                and editor in self.documents and (version is None or self.documents[editor].version == version))

    def current_location(self, editor):
        position = position_at(editor.text(), editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset())
        return CodeLocation(editor.device_id, editor.path, **position)

    def update_links(self, editor):
        document = self.documents[editor]
        if getattr(getattr(editor, 'document', None), 'large', False):
            document.links = []
            editor.buffer.remove_tag(editor.code_link_tag, editor.buffer.get_start_iter(), editor.buffer.get_end_iter())
            return
        device = self.window.workspace.devices[editor.device_id]
        document.links = self.path_parser(editor.text(), posixpath.dirname(editor.path), editor.device_id,
                                    device.navigation_state.home, remote=device.type == "ssh")
        editor.buffer.remove_tag(editor.code_link_tag, editor.buffer.get_start_iter(), editor.buffer.get_end_iter())
        for link in document.links:
            editor.buffer.apply_tag(editor.code_link_tag, editor.buffer.get_iter_at_offset(link.start), editor.buffer.get_iter_at_offset(link.end))

    @staticmethod
    def pointer_offset(editor, x, y):
        bx, by = editor.view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, iterator = editor.view.get_iter_at_location(bx, by)
        return iterator.get_offset() if found else None

    def clicked(self, editor, gesture, x, y):
        if not self.valid(editor) or not gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            return
        offset = self.pointer_offset(editor, x, y)
        if offset is None:
            return
        # Large documents still resolve explicit path clicks on a bounded line,
        # without scheduling a full-document scan or language-server request.
        if getattr(getattr(editor,'document',None),'large',False):
            first = editor.buffer.get_iter_at_offset(offset)
            first.set_line_offset(0)
            last = first.copy()
            last.forward_to_line_end()
            if last.get_offset() - first.get_offset() > 16384: return
            device = self.window.workspace.devices[editor.device_id]
            links = self.path_parser(editor.buffer.get_text(first,last,True),posixpath.dirname(editor.path),
                                     editor.device_id,device.navigation_state.home,remote=device.type=='ssh')
            relative = offset-first.get_offset()
            link = next((link for link in links if link.start <= relative < link.end),None)
        else:
            # A debounced highlight may still be stale.
            self.update_links(editor)
            link = next((link for link in self.documents[editor].links if link.start <= offset < link.end), None)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if link:
            self.path_opener(link.location)
        else:
            self.query(editor, "definition", offset)

    def motion(self, editor, x, y):
        if not self.valid(editor):
            return
        document = self.documents[editor]
        offset = self.pointer_offset(editor, x, y)
        link = next((link for link in document.links if offset is not None and link.start <= offset < link.end), None)
        editor.view.set_cursor_from_name("pointer" if link else "text")
        if offset == document.hover_offset:
            return
        document.hover_offset = offset
        self.sources.remove(document.hover_source)
        editor.view.set_tooltip_text(None)
        if link:
            editor.view.set_tooltip_text(self.describe(link.location) + " — Ctrl+Click")
        elif document.session and offset is not None:
            document.hover_source = self.sources.timeout(600, self.query, editor, "hover", offset, True)

    def leave(self, editor):
        document = self.documents.get(editor)
        if self.closed or not document:
            return
        self.sources.remove(document.hover_source)
        document.hover_offset = None
        editor.view.set_tooltip_text(None)

    def query(self, editor, method, offset=None, automatic=False, new_name=None):
        if not self.valid(editor) or editor.loading:
            return
        if getattr(getattr(editor,'document',None),'large',False):
            if not automatic: editor.code_status.set_text('Large file mode: code analysis disabled.')
            return
        document = self.documents[editor]
        if not document.session:
            editor.code_status.set_text("Pyright code services currently support Python files only.")
            return
        offset = editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset() if offset is None else offset
        session, path, text, version = document.session, editor.path, editor.text(), document.version
        position = position_at(text, offset)
        extra = {"context": {"includeDeclaration": True}} if method == "references" else {}
        if method == "rename":
            extra["newName"] = new_name
        buffers = {opened.path: (self.documents[opened].version, opened.text()) for opened in self.window.editors
                   if opened.device_id == editor.device_id and not opened.loading} if method == "rename" else None
        generation = getattr(editor.backend, "generation", 0)
        if not automatic:
            editor.code_status.set_text("Pyright: " + method + "…")
        def done(result, error):
            if not self.valid(editor, version) or (automatic and document.hover_offset != offset):
                return
            if error:
                if not automatic:
                    editor.code_status.set_text(str(error))
                return
            try:
                if method == "hover":
                    information = self.hover_text(result)
                    if automatic:
                        editor.view.set_tooltip_text(information or None)
                    else:
                        self.show_results(editor, information or "No hover information", [])
                elif method == "rename":
                    self.preview_rename(editor, result, new_name, generation)
                else:
                    locations = self.locations(session, result)
                    if method == "definition" and len(locations) == 1:
                        self.jump(locations[0], origin_location=CodeLocation(editor.device_id, path, **position))
                    else:
                        self.show_results(editor, f"{len(locations)} {method} location(s)", locations)
            except (ValueError, TypeError) as error:
                editor.code_status.set_text(str(error))
        def job():
            if method == "rename":
                return session.rename(path, text, version, position, new_name, buffers)
            return session.query(method, path, text, version, position, **extra)
        self.work(job, done)

    def assist(self, editor, method):
        """Request supported edits, then reuse the revision-checked rename review."""
        from ...development import choose
        from ...code_navigation import apply_text_edits
        if not self.valid(editor) or editor.loading or editor.document.large: return
        document = self.documents[editor]
        session = document.session
        if not session:
            editor.code_status.set_text('No language server for this file. Enable a language plugin in Plugins; Python uses locally installed Pyright.'); return
        text, version = editor.text(), document.version
        path, backend = editor.path, editor.backend
        generation = getattr(backend, 'generation', 0)
        offset = editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset()
        position = position_at(text, offset)
        buffers = {e.path: (self.documents[e].version, e.text()) for e in self.window.editors if e.device_id == editor.device_id and not e.loading}
        extra = {}
        if method == 'formatting': extra['options'] = {'tabSize': editor.view.get_tab_width(), 'insertSpaces': editor.view.get_insert_spaces_instead_of_tabs()}
        if method == 'codeAction': extra = {'range': {'start': position, 'end': position}, 'context': {'diagnostics': list(document.diagnostics), 'only': ['quickfix']}}
        def valid():
            return self.valid(editor, version) and generation == getattr(backend, 'generation', 0)
        def review(edit, title):
            if not valid(): return
            def planned(plan, error):
                if not valid(): return
                if error: editor.code_status.set_text(str(error)); return
                if not plan: editor.code_status.set_text('No changes suggested.'); return
                self.preview_rename(editor, plan, title, generation, title=title)
            self.work(lambda: session.rename_plan(edit, buffers), planned)
        def completion(item):
            if not valid(): return
            if item.get('insertTextFormat', 1) != 1 or item.get('command'):
                editor.code_status.set_text('This completion requires snippets or a server command, which are not supported.'); return
            edit = item.get('textEdit')
            if edit is None:
                start = offset
                while start and (text[start-1].isalnum() or text[start-1] == '_'): start -= 1
                edit = {'range': {'start': position_at(text, start), 'end': position}, 'newText': item.get('insertText', item.get('label', ''))}
            if 'insert' in edit and 'replace' in edit: edit = {'range': edit['replace'], 'newText': edit['newText']}
            edits = [edit] + item.get('additionalTextEdits', [])
            try: content = apply_text_edits(text, edits)
            except (ValueError, TypeError, KeyError) as error: editor.code_status.set_text(str(error)); return
            editor.buffer.begin_user_action()
            try:
                editor.buffer.delete(editor.buffer.get_start_iter(), editor.buffer.get_end_iter())
                editor.buffer.insert(editor.buffer.get_start_iter(), content)
                end = min(len(content), offset + len(content) - len(text))
                editor.buffer.place_cursor(editor.buffer.get_iter_at_offset(max(0,end)))
            finally: editor.buffer.end_user_action()
            editor.view.grab_focus()
        def action(item):
            if item.get('command') or not item.get('edit'):
                editor.code_status.set_text('Only quick fixes containing reviewable text edits are supported.'); return
            review(item['edit'], item.get('title', 'Quick Fix'))
        def done(result, error):
            if not valid(): return
            if error: editor.code_status.set_text(str(error)); return
            if method == 'formatting':
                if not result: editor.code_status.set_text('No formatting changes.'); return
                review({'changes': {session.mapper.uri(path): result}}, 'Format Document')
            elif method == 'completion':
                items = result.get('items', []) if isinstance(result, dict) else result or []
                items = [i for i in items if isinstance(i,dict) and isinstance(i.get('label'),str)][:1000]
                if items:
                    import re
                    prefix = re.search(r'\w*$', text[:offset]).group()
                    choose(self.window, 'Code Completion', [(i['label'],i) for i in items], completion, initial_query=prefix)
                else: editor.code_status.set_text('No completions at this position.')
            else:
                items = [i for i in result or [] if isinstance(i,dict) and i.get('edit') and not i.get('disabled') and not i.get('command')][:100]
                if items: choose(self.window, 'Quick Fixes', [(i.get('title','Quick Fix'),i) for i in items], action)
                else: editor.code_status.set_text('No reviewable quick fixes at this position.')
        editor.code_status.set_text('Language server: ' + method + '…')
        self.work(lambda: session.query(method, path, text, version, position, **extra), done)

    @staticmethod
    def hover_text(result):
        if not isinstance(result, dict):
            return ""
        contents = result.get("contents", "")
        parts = contents if isinstance(contents, list) else [contents]
        return "\n\n".join(part if isinstance(part, str) else str(part.get("value", "")) if isinstance(part, dict) else ""
                           for part in parts)[:20000]

    @staticmethod
    def locations(session, result):
        values = result if isinstance(result, list) else [result] if result else []
        if len(values) > 1000:
            raise ValueError("Navigation result exceeds the 1000-location limit")
        locations = []
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("Invalid code location")
            uri = item.get("targetUri", item.get("uri"))
            span = item.get("targetSelectionRange", item.get("range", {}))
            if not isinstance(span, dict):
                raise ValueError("Invalid code location range")
            locations.append(session.mapper.location(uri, span.get("start")))
        return list(dict.fromkeys(locations))

    def describe(self, location):
        device = self.window.workspace.devices[location.device_id]
        return f"{device.display_name}:{location.path}:{location.line + 1}:{location.character + 1}"

    def show_results(self, editor, heading, locations):
        if not self.valid(editor):
            return
        document = self.documents[editor]
        document.result_signals.close()
        child = editor.code_results.get_first_child()
        while child:
            following = child.get_next_sibling()
            editor.code_results.remove(child)
            child = following
        close = Gtk.Button(label="Close results")
        document.result_signals.connect(close, "clicked", lambda *_: editor.code_results_scroll.set_visible(False))
        editor.code_results.append(close)
        editor.code_results.append(Gtk.Label(label=heading, xalign=0, wrap=True, selectable=True))
        for location in locations:
            button = Gtk.Button(label=self.describe(location))
            button.set_has_frame(False)
            document.result_signals.connect(button, "clicked", lambda *_, location=location: self.jump(location))
            editor.code_results.append(button)
        editor.code_results_scroll.set_visible(True)
        editor.code_status.set_text(heading.splitlines()[0][:500])

    def place(self, editor, location):
        offset = offset_at(editor.text(), {"line": location.line, "character": location.character}, clamp=True)
        iterator = editor.buffer.get_iter_at_offset(offset)
        editor.buffer.place_cursor(iterator)
        editor.view.scroll_to_iter(iterator, 0.1, False, 0, 0)
        editor.view.grab_focus()

    def open_link(self, location):
        """Path links reuse the existing viewer registry, including media tabs."""
        if self.closed:
            return
        device = self.window.workspace.devices.get(location.device_id)
        if not device:
            return
        existing = next((viewer for viewer in self.window.viewers if viewer.device_id == device.id and viewer.path == location.path), None)
        if existing:
            if existing in self.window.editors:
                self.jump(location)
            else:
                self.window.notebook.set_current_page(self.window.notebook.page_num(existing))
            return
        backend, path = device.filesystem_backend, location.path
        generation = getattr(backend, "generation", 0)
        source = self.window.current_editor()
        origin = self.current_location(source) if source and not source.loading else None
        def inspected(probe, error):
            if self.closed:
                return
            if error or generation != getattr(backend, "generation", 0):
                self.window.set_status(str(error) if error else "Connection changed while opening path; retry")
            elif not probe.exists:
                self.window.set_status("File path does not exist: " + path)
            elif self.window.viewer_registry.select(path, probe.sample).kind == "text":
                self.jump(CodeLocation(device.id, probe.path, location.line, location.character), origin_location=origin)
            else:
                self.window.open_path(backend, probe.path, device.session_id)
        self.window.work(lambda: backend.probe(path), inspected)

    def jump(self, location, *, record=True, history_step=None, origin_location=None):
        if self.closed:
            return
        device = self.window.workspace.devices.get(location.device_id)
        if not device:
            self.window.set_status("Navigation device is no longer registered")
            return
        origin = self.window.current_editor()
        if origin_location is None and origin and not origin.loading:
            origin_location = self.current_location(origin)
        backend, generation = device.filesystem_backend, getattr(device.filesystem_backend, "generation", 0)
        if history_step is not None:
            self.history_pending = True
        def ready(snapshot, error):
            if self.closed:
                return
            self.history_pending = False
            if error or generation != getattr(backend, "generation", 0):
                self.window.set_status(str(error) if error else "Connection changed during navigation; retry")
                return
            editor = existing if existing else self.window.open_snapshot(backend, snapshot, device.session_id)
            if editor is None:
                return
            self.window.notebook.set_current_page(self.window.notebook.page_num(editor))
            actual = CodeLocation(device.id, editor.path, location.line, location.character)
            if editor.loading:
                self.documents[editor].pending_location = actual
            else:
                self.place(editor, actual)
            if record:
                if origin_location:
                    self.history.add(origin_location)
                self.history.add(actual)
            elif history_step is not None:
                self.history.accept(history_step)
        existing = next((editor for editor in self.window.editors if editor.device_id == device.id and editor.path == location.path), None)
        if existing:
            # No backend read needed for an existing, possibly disconnected or
            # dirty buffer. Keep its saved revision and unsaved contents intact.
            ready(None, None)
            return
        path = location.path
        def read():
            snapshot = backend.read(path)
            if snapshot.revision == "missing":
                raise FileNotFoundError("Code/path navigation target does not exist: " + path)
            return snapshot
        self.window.work(read, ready)

    def history_move(self, direction):
        if self.closed or self.history_pending:
            return
        target = self.history.target(direction)
        if target:
            self.jump(target, record=False, history_step=direction)

    def rename_dialog(self, editor):
        if not self.valid(editor) or not self.documents[editor].session:
            return
        if self.window.dialog:
            self.window.dialog.present()
            return
        dialog = Gtk.Dialog(title="Rename Python symbol — " + self.window.workspace.devices[editor.device_id].display_name,
                            transient_for=self.window, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Preview", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        entry = Gtk.Entry(placeholder_text="New identifier", activates_default=True)
        dialog.get_content_area().append(entry)
        errors = Gtk.Label(xalign=0, wrap=True)
        dialog.get_content_area().append(errors)
        self.window.dialog = dialog
        def response(dialog, answer):
            if self.closed:
                return
            name = entry.get_text()
            if answer == Gtk.ResponseType.ACCEPT and (not name.isidentifier() or keyword.iskeyword(name)):
                errors.set_text("Enter a valid Python identifier that is not a keyword.")
                return
            self.window.dialog = None
            self.window.signals.disconnect(dialog)
            dialog.destroy()
            if answer == Gtk.ResponseType.ACCEPT:
                self.query(editor, "rename", new_name=name)
        self.window.signals.connect(dialog, "response", response)
        dialog.present()

    def preview_rename(self, editor, plan, new_name, generation, title=None):
        description = f"{title or ('Rename to '+repr(new_name))} on {self.window.workspace.devices[editor.device_id].display_name}.\n"
        description += "\n".join(f"{item.location.path}: {item.edits} edit(s)" for item in plan)
        preview = []
        for item in plan[:3]:
            preview.extend(list(difflib.unified_diff(item.original.splitlines(), item.content.splitlines(),
                                                   fromfile=item.location.path, tofile=item.location.path, n=1))[:8])
        description += "\n\nChange excerpt:\n" + "\n".join(preview)[:3000]
        description += "\n\nApply to unsaved buffers only. Review/undo in each tab; Save uses conflict checks."
        if self.window.dialog:
            self.window.dialog.present()
            editor.code_status.set_text("Close the current dialog and retry rename.")
            return
        dialog = Gtk.Dialog(title=title or "Apply symbol rename?", transient_for=self.window, modal=True)
        dialog.set_default_size(700, 450)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Apply to buffers", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        body = Gtk.Label(label=description, xalign=0, wrap=True, selectable=True)
        body.add_css_class("monospace")
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(body)
        dialog.get_content_area().append(scroll)
        dialog.code_preview_text = description
        self.window.dialog = dialog
        def response(dialog, answer):
            if self.closed:
                return
            self.window.dialog = None
            self.window.signals.disconnect(dialog)
            dialog.destroy()
            if answer == Gtk.ResponseType.ACCEPT:
                self.apply_rename(editor, plan, generation)
        self.window.signals.connect(dialog, "response", response)
        dialog.present()

    def apply_rename(self, origin, plan, generation):
        if not self.valid(origin):
            return
        try:
            if generation != getattr(origin.backend, "generation", 0):
                raise ValueError("Connection changed after rename preview; retry")
            opened = {editor.path: editor for editor in self.window.editors if editor.device_id == origin.device_id}
            if any(item.location.device_id != origin.device_id for item in plan):
                raise ValueError("Rename cannot cross device identities")
            new_count = sum(item.location.path not in opened for item in plan)
            if len(self.window.viewers) + new_count > 64:
                raise ValueError("Close tabs before applying this rename (64-tab limit)")
            for item in plan:
                editor = opened.get(item.location.path)
                if editor and (editor.loading or editor.saving or editor.text() != item.original
                               or item.version is not None and self.documents[editor].version != item.version):
                    raise ValueError("An affected buffer changed after preview; no rename was applied")
                if not editor and item.snapshot is None:
                    raise ValueError("An affected tab closed after preview; retry rename")
            # All targets were checked before changing any buffer. Disk writes
            # remain the existing explicit, revision-checked Ctrl+S operation.
            for item in plan:
                editor = opened.get(item.location.path)
                if editor is None:
                    editor = self.window.open_snapshot(origin.backend, item.snapshot, origin.session)
                editor.buffer.begin_user_action()
                try:
                    # GtkTextBuffer.set_text starts an irreversible action;
                    # it cannot be nested in begin_user_action. Explicit text
                    # operations retain undo and keep GTK's action stack valid.
                    editor.buffer.delete(editor.buffer.get_start_iter(), editor.buffer.get_end_iter())
                    editor.buffer.insert(editor.buffer.get_start_iter(), item.content)
                finally:
                    editor.buffer.end_user_action()
                editor.buffer.set_modified(True)
                editor.code_status.set_text("Changes applied to buffer — review and Save")
        except ValueError as error:
            origin.code_status.set_text(str(error))

    def clear_diagnostics(self, editor):
        document = self.documents[editor]
        document.diagnostics = []
        for category in ('naui-error','naui-warning'):
            editor.buffer.remove_source_marks(editor.buffer.get_start_iter(),editor.buffer.get_end_iter(),category)
        for tag in (editor.code_error_tag, editor.code_warning_tag):
            editor.buffer.remove_tag(tag, editor.buffer.get_start_iter(), editor.buffer.get_end_iter())

    def diagnostics(self, session, params):
        if not isinstance(params, dict):
            return
        location = session.mapper.location(params.get("uri"))
        editor = next((editor for editor, document in self.documents.items()
                       if document.session is session and editor.path == location.path), None)
        if not editor or editor.loading:
            return
        document = self.documents[editor]
        version = params.get("version")
        sent = session.documents.get(editor.path)
        if (version is not None and version != document.version) or not sent or sent[0] != document.version:
            return  # Never underline positions belonging to an older buffer.
        diagnostics = params.get("diagnostics")
        if not isinstance(diagnostics, list):
            return
        self.clear_diagnostics(editor)
        text = editor.text()
        positions = TextPositions(text)
        marked_lines = set()
        for item in diagnostics[:1000]:
            try:
                span = item["range"]
                first, last = positions.offset(span["start"]), positions.offset(span["end"])
                if last < first or not isinstance(item.get("message"), str):
                    continue
                document.diagnostics.append(item)
                tag = editor.code_error_tag if item.get("severity", 1) == 1 else editor.code_warning_tag
                editor.buffer.apply_tag(tag, editor.buffer.get_iter_at_offset(first), editor.buffer.get_iter_at_offset(last))
                iterator = editor.buffer.get_iter_at_offset(first)
                category = 'naui-error' if item.get('severity',1) == 1 else 'naui-warning'
                if (iterator.get_line(),category) not in marked_lines:
                    editor.buffer.create_source_mark(None,category,iterator)
                    marked_lines.add((iterator.get_line(),category))
            except (ValueError, TypeError, KeyError):
                continue
        editor.code_status.set_text(f"Pyright: {len(document.diagnostics)} diagnostic(s)" + (" — " + session.warning if session.warning else ""))

    def diagnostic_tooltip(self, editor, x, y, keyboard, tooltip):
        if not self.valid(editor) or getattr(getattr(editor,'document',None),'large',False): return False
        offset = editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset() if keyboard else self.pointer_offset(editor,x,y)
        if offset is None: return False
        position = position_at(editor.text(),offset)
        point = (position['line'],position['character'])
        messages = []
        for item in self.documents[editor].diagnostics:
            try:
                start,end = item['range']['start'],item['range']['end']
                if (start['line'],start['character']) <= point <= (end['line'],end['character']):
                    messages.append(str(item.get('message',''))[:4000])
            except (KeyError,TypeError): continue
        if not messages: return False
        tooltip.set_text('\n\n'.join(messages[:5]))
        return True

    def show_problems(self, editor):
        if not self.valid(editor):
            return
        items = self.documents[editor].diagnostics
        locations, messages = [], []
        for item in items:
            line, character = validate_position(item["range"]["start"])
            locations.append(CodeLocation(editor.device_id, editor.path, line, character))
            messages.append(f"{line + 1}:{character + 1}: {item['message'][:2000]}")
        self.show_results(editor, "\n".join(messages) or "No current diagnostics", locations)

    def reindex(self, editor):
        if not self.valid(editor) or not self.documents[editor].session:
            return
        session = self.documents[editor].session
        buffers = {opened.path: (document.version, opened.text()) for opened, document in self.documents.items()
                   if document.session is session and not opened.loading}
        def job():
            warning = session.refresh()
            for path, (version, text) in buffers.items():
                session.sync(path, text, version)
            return warning
        self.work(job, lambda warning, error: editor.code_status.set_text(str(error) if error else warning or "Code index refreshed")
                         if self.valid(editor) else None)

    def poll(self):
        self.poll_source = None
        if self.closed:
            return
        for session in self.sessions.values():
            client = session.client
            if not client:
                continue
            for event in client.drain():
                try:
                    if event.get("method") == "textDocument/publishDiagnostics":
                        self.diagnostics(session, event.get("params"))
                    elif event.get("method") == "naui/error":
                        params = event.get("params")
                        if not isinstance(params, dict):
                            continue
                        for editor, document in self.documents.items():
                            if document.session is session:
                                editor.code_status.set_text(str(params.get("message", "Language server failed")))
                except (ValueError, TypeError, KeyError):
                    continue
        self.poll_source = self.sources.timeout(200, self.poll)

    def add_terminal(self, identity, terminal):
        signals = self.terminal_signals[identity] = Signals()
        # VTE accepts PCRE2 flags directly: MULTILINE | UCP | UTF. UCP makes
        # bare Unicode filenames match the same way as Python's path scanner.
        regex = Vte.Regex.new_for_match(self.terminal_path_pattern, -1, 0x00000400 | 0x00020000 | 0x00080000)
        tag = terminal.view.match_add_regex(regex, 0)
        terminal.view.match_set_cursor_name(tag, "pointer")
        click = Gtk.GestureClick()
        click.set_button(1)
        def pressed(gesture, count, x, y):
            if self.closed or terminal.closed or not gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
                return
            matched, matched_tag = terminal.view.check_match_at(x, y)
            if not matched or matched_tag != tag:
                return
            runtime = self.window.runtimes[identity]
            device = self.window.workspace.devices[runtime.device_id]
            if device.type == "ssh" and not self.window.session_is_current(runtime.active_session):
                return
            context = runtime.contexts.get(runtime.active_session, {})
            if not context.get("path"):
                return
            links = self.path_parser(matched, context["path"], device.id, context.get("home"), remote=device.type == "ssh")
            if links:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self.path_opener(links[0].location)
        signals.connect(click, "pressed", pressed)
        terminal.view.add_controller(click)

    def remove_terminal(self, identity):
        signals = self.terminal_signals.pop(identity, None)
        if signals:
            signals.close()

    def relocated(self, editor, old_path):
        document = self.documents.get(editor)
        if not document or self.closed: return
        if document.session:
            session, version = document.session, document.version
            self.work(lambda: session.detach(old_path, version), lambda *_: None)
        document.session = None
        self.loaded(editor)

    def detach(self, editor):
        document = self.documents.pop(editor, None)
        if not document:
            return
        self.sources.remove(document.change_source)
        self.sources.remove(document.hover_source)
        document.result_signals.close()
        if document.session and not self.closed:
            session, path, version = document.session, editor.path, document.version
            self.work(lambda: session.detach(path, version), lambda *_: None)

    def remove_device(self, device_id):
        for key, session in list(self.sessions.items()):
            if key[0] == device_id:
                session.cancel()
                # Cache cleanup can block until a file job returns. The worker
                # receives only the session, never retained editor widgets.
                self.work(session.close, lambda *_: None)
                del self.sessions[key]

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.sources.close()
        self.file_work.stop()
        for document in self.documents.values():
            document.result_signals.close()
        self.documents.clear()
        for signals in self.terminal_signals.values():
            signals.close()
        self.terminal_signals.clear()
        for session in self.sessions.values():
            session.cancel()

    def cleanup(self):
        # File workers are joined before deleting caches they may still write.
        self.close()
        self.file_work.join()
        for session in self.sessions.values():
            session.close()
        self.sessions.clear()
        self.window = None  # Break the Python cycle before GTK disposes Window.

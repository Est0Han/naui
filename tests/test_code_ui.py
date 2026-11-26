"""GTK adapter ownership/rename/diagnostics with real headless source buffers."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from naui.code_navigation import CodeLocation, UriMapper, file_uri
from naui.lsp_workspace import LspSession
from naui.workspace import Workspace
from test_workspace import Label, RootedTransport, wait_for
from lsp_helpers import connect_virtual_machine

try:
    import gi
    gi.require_version('GtkSource', '5')
    gi.require_version('Gtk', '4.0')
    gi.require_version('Vte', '3.91')
    from gi.repository import GtkSource, Pango
    from naui.code_ui import CodeNavigation, Document
    from naui.gui import EditorPane, NauiWindow
    from naui.lifecycle import FileWork, Signals, Sources
except (ImportError, ValueError):
    CodeNavigation = None


class EditorSurface:
    @property
    def dirty(self): return self.buffer.get_modified()
    def text(self): return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)


@unittest.skipIf(CodeNavigation is None, 'GTK bindings unavailable')
class NativeCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = Workspace(str(self.root))
        self.sources, self.signals = Sources(), Signals()
        self.work = FileWork(self.sources)
        self.statuses = []
        self.window = SimpleNamespace(_shutting_down=False, workspace=self.workspace, editors=[], viewers=[],
                                      work=self.work.submit, set_status=self.statuses.append)
        from naui.ui_settings import DEFAULTS
        self.window.ui_settings = SimpleNamespace(values=dict(DEFAULTS))
        self.window.current_editor = lambda: getattr(self.window, 'active_editor', None)
        self.window.open_snapshot = self.open_snapshot
        def switch(index):
            self.window.active_editor = self.window.editors[index]
            self.workspace.active_device_id = self.window.active_editor.device_id
        self.window.notebook = SimpleNamespace(page_num=lambda editor: self.window.editors.index(editor), set_current_page=switch)
        self.ui = self.window.code_navigation = CodeNavigation(self.window)
        self.addCleanup(self.finish)

    def finish(self):
        self.ui.close()
        self.sources.close()
        self.work.stop()
        for device in self.workspace.devices.values():
            if device.type == 'ssh': device.filesystem_backend.close()
        self.work.join()
        self.ui.cleanup()
        self.signals.close()
        self.window.editors.clear(); self.window.viewers.clear()

    def settle(self):
        wait_for(lambda: not self.work._futures and not self.work._callbacks)

    def remote(self, host):
        device = self.workspace.add_ssh(host)
        directory = self.root / host / 'project'
        directory.mkdir(parents=True)
        (directory / 'main.py').write_text('name = 1\nprint(name)\n')
        (directory / 'helper.py').write_text('from main import name\n')
        connect_virtual_machine(device.filesystem_backend, host, directory.parent)
        device.state = 'connected'
        device.observe('/project')
        return device, directory

    def open_snapshot(self, backend, snapshot, session=None):
        for editor in self.window.editors:
            if editor.backend is backend and editor.path == snapshot.path:
                return editor
        device = self.workspace.device_for_backend(backend)
        editor = EditorSurface()
        editor.window, editor.backend, editor.device_id = self.window, backend, device.id
        editor.path, editor.session, editor.revision = snapshot.path, session, snapshot.revision
        editor._shutting_down = editor.loading = editor.saving = editor.disconnected = False
        editor.buffer = GtkSource.Buffer()
        editor.buffer.set_enable_undo(True)
        editor.buffer.set_text(snapshot.content)
        editor.buffer.set_modified(False)
        editor.code_status, editor.status = Label(), Label()
        editor.update_label = lambda: None
        editor.view = SimpleNamespace(scroll_to_iter=lambda *args: None, grab_focus=lambda: None)
        editor.code_link_tag = editor.buffer.create_tag('link', underline=Pango.Underline.SINGLE)
        editor.code_error_tag = editor.buffer.create_tag('error', underline=Pango.Underline.ERROR)
        editor.code_warning_tag = editor.buffer.create_tag('warning', underline=Pango.Underline.SINGLE)
        self.window.editors.append(editor); self.window.viewers.append(editor)
        self.ui.documents[editor] = Document(editor, version=self.ui.next_version())
        self.signals.connect(editor.buffer, 'changed', lambda *_: self.ui.changed(editor))
        editor.save = EditorPane.save.__get__(editor)
        editor.failed = EditorPane.failed.__get__(editor)
        return editor

    def test_navigation_keeps_origin_device_even_after_workspace_switch(self):
        a, _ = self.remote('server-a')
        b, _ = self.remote('server-b')
        origin = self.open_snapshot(a.filesystem_backend, a.filesystem_backend.read('/project/main.py'))
        self.window.active_editor = origin
        self.workspace.active_device_id = b.id
        self.ui.jump(CodeLocation(a.id, '/project/helper.py', 0, 5))
        self.settle()
        target = self.window.active_editor
        self.assertEqual(target.device_id, a.id)
        self.assertIs(target.backend, a.filesystem_backend)
        self.assertEqual(target.buffer.get_iter_at_mark(target.buffer.get_insert()).get_offset(), 5)
        self.assertEqual(self.ui.history.entries[-1].device_id, a.id)
        self.ui.history_move(-1)
        self.assertIs(self.window.active_editor, origin)

    def test_clicking_missing_path_does_not_open_new_file_or_switch_device(self):
        a, _ = self.remote('server-a')
        self.ui.jump(CodeLocation(a.id, '/project/missing.py'))
        self.settle()
        self.assertFalse(self.window.editors)
        self.assertIn('does not exist', self.statuses[-1])

    def test_existing_dirty_buffer_navigation_preserves_contents_and_backend(self):
        a, directory = self.remote('server-a')
        editor = self.open_snapshot(a.filesystem_backend, a.filesystem_backend.read('/project/main.py'))
        editor.buffer.set_text('unsaved = "😀"\n')
        self.ui.jump(CodeLocation(a.id, editor.path, 0, 13))
        self.assertEqual(editor.text(), 'unsaved = "😀"\n')
        self.assertTrue(editor.dirty)
        self.assertEqual((directory / 'main.py').read_text(), 'name = 1\nprint(name)\n')
        self.assertIs(editor.backend, a.filesystem_backend)

    def test_multi_file_rename_is_buffer_only_undoable_and_saves_to_correct_device(self):
        a, directory_a = self.remote('server-a')
        b, directory_b = self.remote('server-b')
        origin = self.open_snapshot(a.filesystem_backend, a.filesystem_backend.read('/project/main.py'))
        other = self.open_snapshot(b.filesystem_backend, b.filesystem_backend.read('/project/main.py'))
        self.workspace.active_device_id = b.id
        session = LspSession(a.id, a.filesystem_backend, '/project', remote=True)
        mirror = self.root / 'mirror'; mirror.mkdir()
        (mirror / 'helper.py').write_text('from main import name\n')
        session.mapper = UriMapper(a.id, '/project', str(mirror))
        def edit(line, start, end):
            return {'range': {'start': {'line': line, 'character': start}, 'end': {'line': line, 'character': end}}, 'newText': 'value'}
        changes = {session.mapper.uri('/project/main.py'): [edit(0, 0, 4), edit(1, 6, 10)],
                   session.mapper.uri('/project/helper.py'): [edit(0, 17, 21)]}
        plan = session.rename_plan({'changes': changes}, {origin.path: (self.ui.documents[origin].version, origin.text())})
        self.ui.apply_rename(origin, plan, a.filesystem_backend.generation)
        helper = next(editor for editor in self.window.editors if editor.device_id == a.id and editor.path.endswith('helper.py'))
        self.assertEqual(origin.text(), 'value = 1\nprint(value)\n')
        self.assertEqual(helper.text(), 'from main import value\n')
        self.assertTrue(origin.dirty and helper.dirty)
        self.assertEqual(other.text(), 'name = 1\nprint(name)\n')
        self.assertIn('name', (directory_a / 'main.py').read_text())
        self.assertTrue(origin.buffer.get_can_undo())
        origin.buffer.undo()
        self.assertEqual(origin.text(), 'name = 1\nprint(name)\n')
        origin.buffer.redo()
        origin.save(); helper.save()
        self.settle()
        self.assertEqual((directory_a / 'main.py').read_text(), 'value = 1\nprint(value)\n')
        self.assertEqual((directory_a / 'helper.py').read_text(), 'from main import value\n')
        self.assertEqual((directory_b / 'main.py').read_text(), 'name = 1\nprint(name)\n')

    def test_stale_rename_and_reconnect_are_rejected_before_any_buffer_changes(self):
        a, _ = self.remote('server-a')
        origin = self.open_snapshot(a.filesystem_backend, a.filesystem_backend.read('/project/main.py'))
        helper = self.open_snapshot(a.filesystem_backend, a.filesystem_backend.read('/project/helper.py'))
        from naui.lsp_workspace import RenameFile
        plan = [RenameFile(CodeLocation(a.id, origin.path), origin.text(), 'new = 2', version=self.ui.documents[origin].version),
                RenameFile(CodeLocation(a.id, helper.path), helper.text(), 'new = 3', version=self.ui.documents[helper].version)]
        helper.buffer.set_text('changed meanwhile')
        original = origin.text()
        self.ui.apply_rename(origin, plan, a.filesystem_backend.generation)
        self.assertEqual(origin.text(), original)
        self.assertIn('changed', origin.code_status.text)
        self.ui.apply_rename(origin, plan, a.filesystem_backend.generation - 1)
        self.assertEqual(origin.text(), original)
        self.assertIn('Connection changed', origin.code_status.text)

    def test_utf16_diagnostics_ignore_stale_versions_and_clear_on_edit(self):
        local = self.workspace.devices['local']
        path = self.root / 'main.py'; path.write_text('😀bad\n')
        editor = self.open_snapshot(local.filesystem_backend, local.filesystem_backend.read(str(path)))
        version = self.ui.documents[editor].version
        session = SimpleNamespace(mapper=UriMapper('local', str(self.root)), documents={editor.path: (version, editor.text())}, warning='')
        self.ui.documents[editor].session = session
        item = {'range': {'start': {'line': 0, 'character': 2}, 'end': {'line': 0, 'character': 5}}, 'message': 'Example type error', 'severity': 1}
        params = {'uri': file_uri(editor.path), 'version': version - 1, 'diagnostics': [item]}
        self.ui.diagnostics(session, params)
        self.assertFalse(self.ui.documents[editor].diagnostics)
        params['version'] = version
        self.ui.diagnostics(session, params)
        self.assertEqual(len(self.ui.documents[editor].diagnostics), 1)
        self.assertIn(editor.code_error_tag, editor.buffer.get_iter_at_offset(1).get_tags())
        self.assertEqual(len(editor.buffer.get_source_marks_at_line(0,'naui-error')),1)
        from unittest.mock import Mock
        tooltip = Mock()
        editor.buffer.place_cursor(editor.buffer.get_iter_at_offset(1))
        self.assertTrue(self.ui.diagnostic_tooltip(editor,0,0,True,tooltip))
        tooltip.set_text.assert_called_with('Example type error')
        editor.buffer.insert_at_cursor('x')
        self.assertFalse(self.ui.documents[editor].diagnostics)
        self.assertNotIn(editor.code_error_tag, editor.buffer.get_iter_at_offset(1).get_tags())
        self.assertFalse(editor.buffer.get_source_marks_at_line(0,'naui-error'))

    def test_keyboard_services_do_not_replace_terminal_keys(self):
        from gi.repository import Gdk
        calls = []
        editor = SimpleNamespace(focus_in_view=lambda: True,loading=False,document=SimpleNamespace(large=False),command=lambda name:calls.append(name))
        window = SimpleNamespace(_shutting_down=False, current_editor=lambda: editor,
                                 code_navigation=SimpleNamespace(query=lambda *args: calls.append(args), rename_dialog=lambda editor: calls.append('rename')))
        from naui.keybindings import CommandRegistry
        from naui.keybinding_host import register_core
        window.command_registry = CommandRegistry()
        register_core(window,window.command_registry)
        context = patch('naui.keybinding_host.focused_context',side_effect=lambda _: 'editor' if editor.focus_in_view() else 'global')
        context.start();self.addCleanup(context.stop)
        self.assertTrue(NauiWindow.key_pressed(window, None, Gdk.KEY_F12, 0, 0))
        self.assertEqual(calls[-1], (editor, 'definition'))
        self.assertTrue(NauiWindow.key_pressed(window, None, Gdk.KEY_F12, 0, Gdk.ModifierType.SHIFT_MASK))
        self.assertEqual(calls[-1], (editor, 'references'))
        self.assertTrue(NauiWindow.key_pressed(window, None, Gdk.KEY_F2, 0, 0))
        self.assertEqual(calls[-1], 'rename')
        self.assertTrue(NauiWindow.key_pressed(window,None,Gdk.KEY_k,0,Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
        self.assertEqual(calls[-1], "delete_line")
        editor.focus_in_view = lambda: False
        window.notebook = SimpleNamespace(get_nth_page=lambda index:None,get_current_page=lambda:0)
        window.terminal = None
        self.assertFalse(NauiWindow.key_pressed(window,None,Gdk.KEY_F12,0,0))

    def test_large_document_never_starts_lsp_or_discards_its_text(self):
        from naui.editor_model import EditorDocument
        from naui.editor_view import GtkBufferAdapter
        device = self.workspace.devices['local']
        path = self.root/'large.py'
        path.write_text('x=1\n'*20001)
        editor = self.open_snapshot(device.filesystem_backend,device.filesystem_backend.read(str(path)))
        editor.document = EditorDocument(device.id,editor.path,editor.backend,GtkBufferAdapter(editor.buffer),editor.revision,large=True)
        original = editor.text()
        self.ui.loaded(editor)
        self.ui.changed(editor)
        self.assertEqual(self.ui.sessions,{})
        self.assertIsNone(self.ui.documents[editor].session)
        self.assertEqual(editor.text(),original)
        self.assertIn('Large file mode',editor.code_status.text)

    def test_native_terminal_click_signal_uses_its_own_device_cwd_and_rejects_old_ssh_lease(self):
        from gi.repository import Gdk
        a, _ = self.remote('server-a')
        b, _ = self.remote('server-b')
        self.workspace.active_device_id = a.id
        b.session_id = 'ssh-b'
        self.window.session_is_current = lambda session: session == b.session_id
        runtime = SimpleNamespace(device_id=b.id, active_session='ssh-b',
                                  contexts={'ssh-b': {'path': '/project/src', 'home': '/home/remote'}})
        self.window.runtimes = {'background-b': runtime}
        controls, opened = [], []
        matched = ['../main.py:7:2']
        view = SimpleNamespace(match_add_regex=lambda regex, flags: 10, match_set_cursor_name=lambda *args: None,
                               add_controller=controls.append, check_match_at=lambda x, y: (matched[0], 10))
        self.ui.add_terminal('background-b', SimpleNamespace(view=view, closed=False))
        self.ui.open_link = opened.append
        gesture = controls[0]
        gesture.get_current_event_state = lambda: Gdk.ModifierType.CONTROL_MASK
        gesture.set_state = lambda state: None  # Signal exercise has no physical event sequence.
        gesture.emit('pressed', 1, 0.0, 0.0)
        self.assertEqual(opened, [CodeLocation(b.id, '/project/main.py', 6, 1)])
        matched[0] = 'https://example.com/main.py'
        gesture.emit('pressed', 1, 0.0, 0.0)
        self.assertEqual(len(opened), 1)
        b.session_id = 'new-ssh-b'
        gesture.emit('pressed', 1, 0.0, 0.0)
        self.assertEqual(len(opened), 1)

    def test_binary_path_links_select_existing_media_viewer_on_origin_device(self):
        from naui.viewer_registry import ViewerRegistry
        a, directory = self.remote('server-a')
        b, _ = self.remote('server-b')
        (directory / 'photo.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 16)
        opened = []
        self.window.viewer_registry = ViewerRegistry()
        self.window.open_path = lambda *args: opened.append(args)
        self.workspace.active_device_id = b.id
        self.ui.open_link(CodeLocation(a.id, '/project/photo.png'))
        self.settle()
        self.assertEqual(len(opened), 1)
        self.assertIs(opened[0][0], a.filesystem_backend)
        self.assertEqual(opened[0][1], '/project/photo.png')
        self.assertFalse(self.window.editors)

    def test_blocked_language_work_does_not_occupy_filesystem_workers(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        completed = []
        def blocked():
            entered.set()
            release.wait(3)
        try:
            self.ui.work(blocked, lambda *_: None)
            self.assertTrue(entered.wait(1))
            self.window.work(lambda: 'filesystem completed', lambda result, error: completed.append(result))
            wait_for(lambda: bool(completed))
            self.assertEqual(completed, ['filesystem completed'])
        finally:
            release.set()

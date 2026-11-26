"""Headless panel contracts, real list models/buffers and device navigation."""
import queue
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]:gi.require_version(name,version)
from gi.repository import Gio, Gtk, GtkSource
from naui.backends import FileSnapshot, LocalFileBackend
from naui.keybindings import CommandRegistry
from naui.keybinding_host import focused_context, register_core
from naui.search_model import SearchQuery, SearchResult, SearchRoot
from naui.search_service import SearchSession
from naui.search_ui import SearchNode, SearchPanel, workspace_roots
from naui.workspace import Device, WorkspaceRoot


class SearchUITests(unittest.TestCase):
    def setUp(self):
        self.backend=LocalFileBackend()
        self.local=Device('local','local','Workstation','localhost',None,self.backend)
        self.remote=Device('remote-a','ssh','Example Server','example.invalid',None,SimpleNamespace(connected=True,generation=0),state='connected')
        for device in (self.local,self.remote):
            device.roots=[WorkspaceRoot('/project/nested'),WorkspaceRoot('/project'),WorkspaceRoot('/other')]
            device.navigation_state.current_path='/project/nested/src'
        self.workspace=SimpleNamespace(devices={'local':self.local,'remote-a':self.remote},active_device_id='remote-a')

    def test_workspace_scopes_and_nested_roots(self):
        roots=workspace_roots(self.workspace)
        self.assertEqual({(root.device_id,root.path) for root in roots},{('local','/project'),('local','/other'),('remote-a','/project'),('remote-a','/other')})
        self.assertTrue(all(root.remote for root in workspace_roots(self.workspace,'device')))
        self.assertEqual([(root.device_id,root.path) for root in workspace_roots(self.workspace,'root')],[('remote-a','/project/nested')])
        self.assertEqual(workspace_roots(self.workspace,'folder','/project/nested/src')[0].path,'/project/nested/src')
        self.assertEqual(workspace_roots(self.workspace,'folder'),())

    def test_single_group_identity_ignores_display_relative_path(self):
        a=SearchResult('local','/project','/project/src/main.py',1,0,0,4,'text')
        b=SearchResult('remote-a','/project','/project/src/main.py',1,0,0,4,'text')
        self.assertNotEqual(a.identity,b.identity)
        group=SearchNode(a,'main.py',True);group.children.append(SearchNode(a,'1 text'))
        group.children.append(SearchNode(a,'2 text'));group.count=2
        self.assertEqual(group.children.get_n_items(),2)
        self.assertEqual(group.count,2)

    def test_result_selection_unicode_character_offsets(self):
        buffer=GtkSource.Buffer();buffer.set_text('first\n😀 café connect\nlast\n')
        editor=SimpleNamespace(buffer=buffer,view=Mock())
        result=SearchResult('remote-a','/project','/project/main.py',2,7,7,14,'😀 café connect')
        SearchPanel.place_match(editor,result)
        selected=buffer.get_selection_bounds()
        self.assertEqual(buffer.get_text(*selected,False),'connect')
        editor.view.grab_focus.assert_called_once()
        # A deleted/shortened line clamps selection rather than throwing.
        SearchPanel.place_match(editor,SearchResult('local','/project','/project/main.py',100,500,500,800,'x'))

    def test_editor_navigation_existing_dirty_buffer_deduplicates(self):
        editor=SimpleNamespace(device_id='remote-a',path='/project/main.py',loading=False,dirty=True)
        window=SimpleNamespace(workspace=self.workspace,editors=[editor],notebook=Mock(),set_status=Mock(),_shutting_down=False)
        panel=SimpleNamespace(closed=False,navigation_request=0,window=window,place_match=Mock())
        result=SearchResult('remote-a','/project','/project/main.py',4,2,2,8,'match')
        SearchPanel.open_value(panel,result)
        panel.place_match.assert_called_once_with(editor,result)
        self.assertTrue(editor.dirty)
        window.notebook.set_current_page.assert_called_once()

    def test_navigation_reads_correct_device_and_late_callback_ignored(self):
        remote_backend=Mock(connected=True,generation=0)
        self.remote.filesystem_backend=remote_backend
        snapshot=FileSnapshot('/project/main.py','example','revision')
        remote_backend.read.return_value=snapshot
        editor=SimpleNamespace()
        window=SimpleNamespace(workspace=self.workspace,editors=[],work=Mock(),set_status=Mock(),_shutting_down=False,
                               open_snapshot=Mock(return_value=editor))
        panel=SimpleNamespace(closed=False,navigation_request=0,window=window,place_match=Mock())
        result=SearchResult('remote-a','/project','/project/main.py',4,2,2,8,'match')
        SearchPanel.open_value(panel,result)
        read,ready=window.work.call_args.args
        self.assertIs(read(),snapshot)
        remote_backend.read.assert_called_once_with('/project/main.py')
        ready(snapshot,None)
        window.open_snapshot.assert_called_once_with(remote_backend,snapshot,self.remote.session_id)
        SearchPanel.open_value(panel,result);late=window.work.call_args.args[1]
        panel.navigation_request+=1;late(snapshot,None)
        self.assertEqual(window.open_snapshot.call_count,1)

    def test_disconnect_and_backend_generation_changes_do_not_open(self):
        self.remote.state='disconnected'
        window=SimpleNamespace(workspace=self.workspace,editors=[],work=Mock(),set_status=Mock(),_shutting_down=False,open_snapshot=Mock())
        panel=SimpleNamespace(closed=False,navigation_request=0,window=window,place_match=Mock())
        result=SearchResult('remote-a','/project','/project/main.py',1,0,0,1,'x')
        SearchPanel.open_value(panel,result);window.work.assert_not_called()
        self.remote.state='connected'
        SearchPanel.open_value(panel,result);ready=window.work.call_args.args[1]
        self.remote.filesystem_backend.generation=1;ready(FileSnapshot(result.logical_path,'x','r'),None)
        window.open_snapshot.assert_not_called()

    def test_debounce_immediately_cancels_and_replaces_only_own_source(self):
        panel=SimpleNamespace(closed=False,service=Mock(),session=object(),sources=Mock(),debounce=7,
            mode=SimpleNamespace(get_active_id=lambda:'content'),regex=Mock(),word=Mock(),search=Mock())
        SearchPanel.changed(panel)
        panel.service.cancel.assert_called_once();self.assertIsNone(panel.session)
        panel.sources.remove.assert_called_once_with(7)
        panel.sources.timeout.assert_called_once_with(220,panel.search)

    def test_search_commands_are_configurable_and_terminal_eof_preserved(self):
        registry=CommandRegistry();search=Mock()
        window=SimpleNamespace(current_editor=lambda:None,search_panel=search,show_sidebar_panel=Mock())
        register_core(window,registry)
        self.assertTrue(registry.dispatch('Ctrl+Shift+F','global'));search.focus_search.assert_called_with('content')
        self.assertTrue(registry.dispatch('Ctrl+P','global'));search.focus_search.assert_called_with('files')
        self.assertTrue(registry.dispatch('Escape','search'));search.cancel.assert_called_once()
        self.assertFalse(registry.dispatch('Ctrl+D','terminal'))
        self.assertFalse(registry.dispatch('Escape','terminal'))
        registry.set('search.files','F9');self.assertFalse(registry.dispatch('Ctrl+P','global'))
        self.assertTrue(registry.dispatch('F9','global'))

    def test_focused_search_uses_search_context_even_with_entry_native_child(self):
        panel=Mock();focus=Mock();focus.is_ancestor.return_value=True
        window=SimpleNamespace(get_focus=lambda:focus,search_panel=panel)
        self.assertEqual(focused_context(window),'search')

    def test_panel_close_is_idempotent_stops_owned_sources_and_observer(self):
        panel=SimpleNamespace(closed=False,cancel=Mock(),sources=Mock(),signals=Mock(),service=Mock(),watch=Mock(),
            popovers={},row_bindings={})
        SearchPanel.close(panel);SearchPanel.close(panel)
        panel.cancel.assert_called_once();panel.service.close.assert_called_once()
        panel.sources.close.assert_called_once();panel.signals.close.assert_called_once();panel.watch.close.assert_called_once()


if __name__ == '__main__':unittest.main()

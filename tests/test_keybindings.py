import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import gi
gi.require_version('Gtk','4.0')
gi.require_version('Gdk','4.0')
gi.require_version('GtkSource','5')
gi.require_version('Vte','3.91')

from naui.keybindings import Command, CommandRegistry, Conflict, normalize


class KeybindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'keybindings.json'
        self.registry=CommandRegistry(self.path)
        self.calls=[]
        self.add('editor.find','editor','Ctrl+F')
        self.add('editor.goto_line','editor','Ctrl+G')
        self.add('sidebar.toggle','global','Ctrl+B')
    def add(self,identity,context,default,**kw):
        self.registry.register(Command(identity,identity,'Test',context,lambda:self.calls.append(identity),default,**kw))
    def test_default_dispatch_and_enabled(self):
        self.assertTrue(self.registry.dispatch('Ctrl+F','editor'))
        self.registry.commands['editor.find'].enabled=lambda:False
        self.assertFalse(self.registry.dispatch('Ctrl+F','editor'))
        self.assertEqual(self.calls,['editor.find'])
    def test_override_clear_reset_and_persistence(self):
        self.registry.set('editor.find','F6')
        self.assertFalse(self.registry.dispatch('Ctrl+F','editor'))
        self.assertTrue(self.registry.dispatch('F6','editor'))
        restored=CommandRegistry(self.path);restored.register(self.registry.commands['editor.find'])
        self.assertEqual(restored.binding('editor.find'),'F6')
        self.registry.set('editor.find',None)
        self.assertIsNone(self.registry.binding('editor.find'))
        self.registry.reset('editor.find');self.assertEqual(self.registry.binding('editor.find'),'Ctrl+F')
        self.registry.set('sidebar.toggle','F8');self.registry.reset_all();self.assertFalse(self.registry.overrides)
    def test_conflict_cancel_and_replace(self):
        with self.assertRaises(Conflict):self.registry.set('editor.find','Ctrl+G')
        self.assertFalse(self.registry.overrides)
        self.registry.set('editor.find','Ctrl+G',replace=True)
        self.assertIsNone(self.registry.binding('editor.goto_line'))
        self.assertEqual(self.registry.binding('editor.find'),'Ctrl+G')
    def test_specific_context_precedes_global(self):
        self.add('global.find','global','Ctrl+F')
        self.add('tree.find','file_tree','Ctrl+F')
        self.assertEqual(self.registry.resolve('Ctrl+F','editor').id,'editor.find')
        self.assertEqual(self.registry.resolve('Ctrl+F','terminal').id,'global.find')
        self.assertEqual(self.registry.resolve('Ctrl+F','file_tree').id,'tree.find')
        self.assertEqual(len(self.registry.conflicts('editor.find','Ctrl+F',overlap=True)),1)
    def test_normalization_and_invalid(self):
        self.assertEqual(normalize('shift+control+p'),'Ctrl+Shift+P')
        self.assertEqual(normalize('Ctrl++'),'Ctrl+Shift+=' )
        self.assertEqual(normalize('Super+f12'),'Super+F12')
        self.assertEqual(normalize('Ctrl+ü'),'Ctrl+Ü')
        self.assertEqual(normalize('Ctrl+ß'),'Ctrl+ß')
        for value in ['', 'Ctrl+Ctrl+F','Ctrl+Unknown','Hyper+P','Ctrl+',5]:
            with self.subTest(value=value),self.assertRaises(ValueError):normalize(value)
    def test_terminal_control_keys_are_protected_even_from_explicit_commands(self):
        for key in ('C','D','Z'):
            self.add('terminal.'+key,'terminal','Ctrl+'+key)
            self.add('global.'+key,'global','Ctrl+'+key)
            self.assertIsNone(self.registry.resolve('Ctrl+'+key,'terminal'))
        self.assertEqual(self.registry.resolve('Ctrl+D','editor').id,'global.D')
    def test_plugin_disable_reenable_restores_override_and_duplicate_defaults_unbound(self):
        for identity in ['plugin-a.hello','plugin-b.hello']:self.add(identity,'plugin','F7',owner=identity.split('.')[0])
        self.assertIsNone(self.registry.resolve('F7','plugin'))
        self.registry.set('plugin-a.hello','F8')
        self.assertEqual(self.registry.resolve('F7','plugin').id,'plugin-b.hello')
        self.registry.unregister_owner('plugin-a');self.registry.unregister_owner('plugin-a')
        self.assertIsNone(self.registry.resolve('F8','plugin'))
        self.add('plugin-a.hello','plugin','F7',owner='plugin-a')
        self.assertEqual(self.registry.resolve('F8','plugin').id,'plugin-a.hello')
    def test_export_import_unknown_and_no_unrelated_settings(self):
        profile=Path(self.temp.name)/'export.json'
        self.registry.set('editor.find','F6');self.registry.export(profile);self.registry.reset_all()
        data=json.loads(profile.read_text());self.assertEqual(set(data),{'version','overrides'})
        data['overrides']['absent.hello']='F9';profile.write_text(json.dumps(data))
        self.assertEqual(self.registry.import_profile(profile),['absent.hello'])
        self.assertEqual(self.registry.binding('editor.find'),'F6')
    def test_import_invalid_or_conflicting_is_transactional(self):
        self.path.write_text(json.dumps({'version':1,'overrides':{'editor.find':'Ctrl+G'}}))
        with self.assertRaises(ValueError):self.registry.import_profile(self.path)
        self.assertFalse(self.registry.overrides)
        self.registry.import_profile(self.path,accept_conflicts=True)
        self.assertIsNone(self.registry.resolve('Ctrl+G','editor'))
        self.path.write_text(json.dumps({'version':1,'overrides':{'editor.find':'garbage'}}))
        with self.assertRaises(ValueError):self.registry.import_profile(self.path)
    def test_bad_config_falls_back_with_warning(self):
        self.path.write_text(json.dumps({'version':1,'overrides':{'editor.find':'garbage','sidebar.toggle':'F8'}}))
        registry=CommandRegistry(self.path);registry.register(self.registry.commands['editor.find'])
        self.assertEqual(registry.binding('editor.find'),'Ctrl+F');self.assertTrue(registry.warnings)
        self.assertEqual(registry.overrides['sidebar.toggle'],'F8')
    def test_callback_exception_isolated(self):
        errors=[];self.registry.on_error=lambda e:errors.append(str(e))
        def fail():raise RuntimeError('test failure')
        self.registry.commands['editor.find'].callback=fail
        self.assertTrue(self.registry.dispatch('Ctrl+F','editor'));self.assertEqual(errors,['test failure'])
    def test_gdk_translation(self):
        from gi.repository import Gdk
        from naui.keybinding_host import event_shortcut
        ctrl=Gdk.ModifierType.CONTROL_MASK;shift=Gdk.ModifierType.SHIFT_MASK
        self.assertEqual(event_shortcut(Gdk.KEY_P,ctrl|shift),'Ctrl+Shift+P')
        self.assertEqual(event_shortcut(Gdk.KEY_question,ctrl|shift),'Ctrl+Shift+/')
        self.assertEqual(event_shortcut(Gdk.KEY_ISO_Left_Tab,0),'Shift+Tab')
        self.assertEqual(event_shortcut(Gdk.KEY_F12,shift),'Shift+F12')
        self.assertIsNone(event_shortcut(Gdk.KEY_Control_L,ctrl))
    def test_plugin_public_registration_unload_and_permission(self):
        from naui.plugin_manager import PluginManager
        from naui.plugin_manifest import PluginSpec
        from naui.plugin_api import PluginAPI
        host=SimpleNamespace(_shutting_down=False)
        host.plugins=PluginManager(host)
        api=PluginAPI(host,'test-plugin',{'commands'})
        identity=api.commands.register('hello','Hello',lambda:self.calls.append('hello'),default_shortcut='F8',context='plugin')
        registry=host.plugins.registry
        registry.set(identity,'F9');self.assertTrue(registry.dispatch('F9','plugin'))
        api.close();api.close();self.assertIsNone(registry.resolve('F9','plugin'))
        api=PluginAPI(host,'test-plugin',{'commands'})
        api.commands.register('hello','Hello',lambda:self.calls.append('again'),default_shortcut='F8',context='plugin')
        self.assertTrue(registry.dispatch('F9','plugin'));api.close()
        restricted=PluginAPI(host,'restricted',set())
        with self.assertRaises(PermissionError):restricted.commands.register('hello','Hello',lambda:None)
    def test_dispatch_uses_registry_after_runtime_change(self):
        from naui.gui import NauiWindow
        from gi.repository import Gdk
        window=SimpleNamespace(_shutting_down=False,command_registry=self.registry)
        with patch('naui.keybinding_host.focused_context',return_value='editor'):
            self.assertTrue(NauiWindow.key_pressed(window,None,Gdk.KEY_f,0,Gdk.ModifierType.CONTROL_MASK))
            self.registry.set('editor.find','F6')
            self.assertTrue(NauiWindow.key_pressed(window,None,Gdk.KEY_f,0,Gdk.ModifierType.CONTROL_MASK))
            self.assertEqual(self.calls,['editor.find'])  # Old GTK caret action was suppressed.
            self.assertTrue(NauiWindow.key_pressed(window,None,Gdk.KEY_F6,0,0))

    def test_unbound_and_stale_commands_cannot_dispatch(self):
        self.add('unbound.command','editor',None)
        self.assertFalse(self.registry.dispatch(None,'editor'))
        self.registry.unregister_owner(None)
        with self.assertRaises(ValueError):self.registry.set('editor.find','F8')
        with self.assertRaises(ValueError):self.registry.reset('editor.find')

    def test_failed_write_does_not_change_live_bindings(self):
        with patch.object(self.registry,'write_profile',side_effect=OSError('test write failure')):
            with self.assertRaises(OSError):self.registry.set('editor.find','F8')
        self.assertEqual(self.registry.binding('editor.find'),'Ctrl+F')
        self.assertFalse(self.registry.overrides)

    def test_safe_mode_uses_core_commands_with_harmless_unknown_plugin_override(self):
        self.path.write_text(json.dumps({'version':1,'overrides':{'absent-plugin.hello':'F7','editor.find':'F8'}}))
        registry=CommandRegistry(self.path)
        registry.register(Command('editor.find','Find','Editor','editor',lambda:None,'Ctrl+F'))
        self.assertEqual(registry.resolve('F8','editor').id,'editor.find')
        self.assertIsNone(registry.resolve('F7','plugin'))
        registry.close();registry.close()
        self.assertIsNone(registry.resolve('F8','editor'))

    def test_actual_focus_context_selection_and_text_field_exemption(self):
        from naui.keybinding_host import focused_context
        class Node:
            def __init__(self,parent=None):self.parent=parent
            def is_ancestor(self,target):return self.parent is target
        class Entry(Node):pass
        class Text(Node):pass
        class Terminal(Node):pass
        sidebar=Node();terminal=Terminal()
        page=SimpleNamespace(view=terminal)
        editor=SimpleNamespace(focus_in_view=lambda:False)
        window=SimpleNamespace(get_focus=lambda:terminal,current_editor=lambda:editor,workspace_sidebar=sidebar,
            notebook=SimpleNamespace(get_current_page=lambda:0,get_nth_page=lambda _:page))
        with patch('naui.keybinding_host.Gtk',SimpleNamespace(Entry=Entry,Text=Text)),patch('naui.keybinding_host.Vte',SimpleNamespace(Terminal=Terminal)):
            self.assertEqual(focused_context(window),'terminal')
            editor.focus_in_view=lambda:True
            self.assertEqual(focused_context(window),'editor')
            window.get_focus=lambda:Entry(sidebar)
            self.assertEqual(focused_context(window),'global')  # Inline rename retains ordinary text keys.
            editor.focus_in_view=lambda:False
            window.get_focus=lambda:Node(sidebar)
            self.assertEqual(focused_context(window),'file_tree')
            window.get_focus=lambda:Node()
            page.keybinding_context='git'
            self.assertEqual(focused_context(window),'git')
            page.keybinding_context='plugin'
            self.assertEqual(focused_context(window),'plugin')

    def test_terminal_close_binding_cannot_close_a_hidden_terminal_from_editor(self):
        from naui.keybinding_host import register_core
        calls=[]
        window=SimpleNamespace(current_editor=lambda:None,close_active_terminal=lambda:calls.append('closed'))
        registry=CommandRegistry();register_core(window,registry)
        registry.set('terminal.close','F8')
        self.assertFalse(registry.dispatch('F8','editor'))
        self.assertFalse(calls)
        self.assertTrue(registry.dispatch('F8','terminal'))
        self.assertEqual(calls,['closed'])

    def test_settings_reset_requires_confirmation_when_default_conflicts(self):
        from naui.keybinding_ui import KeybindingsView
        self.registry.set('editor.find','F8')
        self.registry.set('editor.goto_line','Ctrl+F')
        prompts=[];messages=[]
        view=SimpleNamespace(registry=self.registry,perform=lambda callback:callback(),status=SimpleNamespace(set_text=messages.append),
            window=SimpleNamespace(confirm_action=lambda *args:prompts.append(args)))
        KeybindingsView.reset(view,'editor.find')
        self.assertEqual(self.registry.binding('editor.find'),'F8')  # Cancel leaves original intact.
        self.assertEqual(prompts[0][2],'Replace Existing')
        prompts[0][3]()
        self.assertEqual(self.registry.binding('editor.find'),'Ctrl+F')
        self.assertIsNone(self.registry.binding('editor.goto_line'))

    def test_settings_import_reports_unknown_and_reviews_conflicts(self):
        from naui.keybinding_ui import KeybindingsView
        profile=Path(self.temp.name)/'import.json'
        profile.write_text(json.dumps({'version':1,'overrides':{'editor.find':'Ctrl+G','missing.hello':'F8'}}))
        prompts=[];messages=[]
        view=SimpleNamespace(_shutting_down=False,registry=self.registry,status=SimpleNamespace(set_text=messages.append),
            window=SimpleNamespace(confirm_action=lambda *args:prompts.append(args)))
        KeybindingsView.import_file(view,profile)
        self.assertFalse(self.registry.overrides)
        prompts[0][3]()
        self.assertIsNone(self.registry.resolve('Ctrl+G','editor'))
        self.assertIn('missing.hello',messages[-1])

"""Public panel permission, identity, schema and lifetime boundaries."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
import gi
for name,version in [('Gtk','4.0'),('GtkSource','5')]:gi.require_version(name,version)
from naui.plugin_api import PluginAPI
from naui.plugin_panels import validate_panel
from naui.ui_settings import UISettings

class PanelAPITests(TestCase):
    def test_conversation_validation_and_escaped_markup(self):
        from naui.conversation_view import validate_messages, markup
        validate_messages([{'id':'one','role':'assistant','text':'**Hello** `code`'}])
        for messages in ([{'id':'one','role':'invalid','text':'x'}],
                         [{'id':'one','role':'user','text':'x'}]*2,
                         [{'id':'one','role':'user','text':'x'*64001}]):
            with self.assertRaises(ValueError): validate_messages(messages)
        self.assertEqual(markup('**Hello**'), '<b>Hello</b>')
        self.assertNotIn('<a ', markup('<a href="file:///secret">bad</a>'))
        self.assertIn('&lt;script&gt;', markup('```html\n<script>\n```'))

    def test_toolbar_permissions_validation_and_lifecycle(self):
        host=self.host(); callback=Mock()
        rows=[{'id':'model','text':'Model','children':[{'text':'Choice','select':callback}]}]
        api=PluginAPI(host,'one',{'views'})
        with self.assertRaises(PermissionError):api.ui.panel('chat','Chat',header_rows=rows)
        api=PluginAPI(host,'one',{'views','commands'})
        self.assertTrue(api.ui.supports('panel_toolbars'))
        api.ui.panel('chat','Chat',composer_rows=rows)
        wrapped=host.right_panels.publish.call_args.kwargs['composer_rows'][0]['children'][0]['select']
        api.quiescing=True;wrapped();callback.assert_not_called()
        api.quiescing=False
        with self.assertRaises(ValueError):api.ui.panel('chat','Chat',header_rows=rows*9)
        with self.assertRaises(ValueError):api.ui.panel('chat','Chat',composer_rows=[{'text':'x','select':'bad'}])

    def test_message_actions_require_commands(self):
        host=self.host();api=PluginAPI(host,'one',{'views'})
        messages=[{'id':'approval','role':'notice','text':'Read?', 'actions':[('Allow', Mock())]}]
        with self.assertRaises(PermissionError): api.ui.panel('chat','Agent',messages=messages)
        api=PluginAPI(host,'one',{'views','commands'})
        api.ui.panel('chat','Agent',messages=messages)
        callback=host.right_panels.publish.call_args.kwargs['messages'][0]['actions'][0][1]
        api.closed=True;callback();messages[0]['actions'][0][1].assert_not_called()

    def host(self):return SimpleNamespace(_shutting_down=False,right_panels=Mock(),sources=Mock(),plugins=SimpleNamespace(fail=Mock()))
    def test_permissions_and_capability_discovery(self):
        host=self.host();api=PluginAPI(host,'one',set())
        self.assertTrue(api.ui.supports('right_panels'));self.assertFalse(api.ui.supports('terminal_panels'))
        with self.assertRaises(PermissionError):api.ui.panel('chat','Agent')
        api=PluginAPI(host,'one',{'views'})
        api.ui.panel('chat','Agent',text='Hello')
        with self.assertRaises(PermissionError):api.ui.panel('chat','Agent',on_submit=Mock())
        host.right_panels.publish.assert_called_once()
    def test_owner_scoping_and_guarded_submit(self):
        host=self.host();api=PluginAPI(host,'one',{'views','commands'});callback=Mock()
        api.ui.panel('chat','Agent',on_submit=callback)
        args=host.right_panels.publish.call_args.args
        self.assertEqual(args[:2],('one','chat'))
        args[5]('hello');callback.assert_called_once_with('hello')
        api.quiescing=True;args[5]('late');self.assertEqual(callback.call_count,1)
        self.assertFalse(args[7]())
    def test_focus_and_removal_cannot_target_other_plugins(self):
        host=self.host();api=PluginAPI(host,'one',{'views'})
        api.ui.focus_panel('chat');host.right_panels.focus_panel.assert_called_once_with('one','chat')
        api.ui.remove_panel('chat');host.right_panels.remove.assert_not_called()
        host.sources.idle.call_args.args[0]();host.right_panels.remove.assert_called_once_with('one','chat')
    def test_invalid_schema_rejected_before_native_calls(self):
        host=self.host();api=PluginAPI(host,'one',{'views','commands'})
        for kwargs in ({'text':'x'*(256*1024+1)}, {'text':'bad\0text'}, {'on_submit':'callback'},
                       {'rows':[{'text':'x','actions':[('Run','not callable')]}]},
                       {'rows':[{'text':'x'}]*201}, {'rows':[{'text':'x','unknown':True}]}):
            with self.assertRaises(ValueError):api.ui.panel('chat','Agent',**kwargs)
        with self.assertRaises(ValueError):api.ui.panel('other:chat','Agent')
        host.right_panels.publish.assert_not_called()
    def test_right_layout_preferences_are_bounded(self):
        UISettings.validate({'right_panel_width':500,'right_panel_collapsed':True,'right_panel_selected':'one:chat'})
        with self.assertRaises(ValueError):UISettings.validate({'right_panel_width':10000})
        with self.assertRaises(ValueError):UISettings.validate({'right_panel_selected':'bad\0:chat'})

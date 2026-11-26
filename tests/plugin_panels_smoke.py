#!/usr/bin/env python3
"""Real GTK panel ownership, input, resize, presets, restart and teardown."""
import os
from pathlib import Path
import tempfile
import time
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]:gi.require_version(name,version)
from gi.repository import Gtk,Gdk,GLib
from naui.gui import NauiApplication
from naui.plugin_manager import PluginSpec
from naui.ui_settings import UISettings
from naui.keybinding_host import focused_context

def text(view):
    buffer=view.get_buffer();return buffer.get_text(buffer.get_start_iter(),buffer.get_end_iter(),True)

def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:return 77
    with tempfile.TemporaryDirectory(prefix='naui-panels-') as directory:
        root=Path(directory);os.environ.update(XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),SHELL='/bin/bash')
        file=root/'example.txt';file.write_text('original\n')
        for run in range(2):
            app=NauiApplication([],directory);state={'step':0};errors=[];messages=[];deadline=time.monotonic()+35
            def tick():
                w=app.window
                if w is None:return True
                try:
                    assert time.monotonic()<deadline,state
                    host=w.right_panels
                    if state['step']==0 and w.at_prompt:
                        grants=frozenset({'views','commands'})
                        def register(name):return w.plugins.activate(PluginSpec(name,name,grants),lambda api:None,grants)
                        first,second=register('panel-one'),register('panel-two')
                        state.update(first=first,second=second)
                        first.ui.panel('chat','Agent One',text='First transcript',on_submit=messages.append)
                        second.ui.panel('chat','Agent Two',text='Second transcript',on_submit=messages.append)
                        assert len(host.panels)==2
                        if run:
                            assert host.choice.get_active_id()=='panel-two:chat'
                            assert not host.get_visible(), 'Publishing must respect saved hide choice'
                            assert w.ui_settings.values['right_panel_width']==420
                            first.ui.focus_panel('chat');state['step']=6
                        else:
                            w.open_file(w.local_backend,str(file));state['step']=1
                    elif state['step']==1 and w.editors and not w.editors[0].loading:
                        editor=w.editors[0];editor.buffer.insert(editor.buffer.get_end_iter(),'unsaved\n')
                        state['editor']=editor;state['pty']=w.terminal.bridge.process
                        panel=host.panels['panel-one:chat'];panel.input.get_buffer().set_text('Unsent draft')
                        state['first'].ui.panel('chat','Agent One Updated',text='New transcript',on_submit=messages.append)
                        assert text(panel.input)=='Unsent draft'
                        state['first'].ui.focus_panel('chat');state['step']=2
                    elif state['step']==2:
                        assert focused_context(w)=='plugin'
                        assert not w.key_pressed(None,Gdk.KEY_s,0,Gdk.ModifierType.CONTROL_MASK)
                        assert state['editor'].dirty
                        host.pane.set_position(host.pane.get_width()-420)
                        panel=host.panels['panel-one:chat']
                        panel.submit();state['step']=3
                    elif state['step']==3 and messages:
                        assert messages==['Unsent draft']
                        state['second'].ui.focus_panel('chat')
                        w.layout_presets.store.save('Agent workspace',w.layout_presets.capture())
                        w.right_panel_button.emit('clicked')
                        assert not host.get_visible() and not w.right_panel_button.get_active()
                        state['second'].ui.panel('chat','Agent Two',text='Background update',on_submit=messages.append)
                        assert not host.get_visible()
                        w.right_panel_button.emit('clicked')
                        assert host.get_visible() and w.right_panel_button.get_active()
                        w.layout_presets.apply('Agent workspace');state['step']=4
                    elif state['step']==4:
                        assert host.get_visible() and host.choice.get_active_id()=='panel-two:chat'
                        assert abs(host.get_width()-420)<20,host.get_width()
                        assert state['editor'].dirty and state['pty'].poll() is None
                        # Queue input then immediately deactivate its owner: no late invocation.
                        first_panel=host.panels['panel-one:chat']
                        first_panel.input.get_buffer().set_text('must not run');first_panel.submit()
                        w.plugins.deactivate('panel-one')
                        assert 'panel-one:chat' not in host.panels and 'panel-two:chat' in host.panels
                        host.hide_panel();state['step']=5
                    elif state['step']==5:
                        assert messages==['Unsent draft']
                        w.save_ui();saved=UISettings(w.ui_settings.path)
                        assert saved.values['right_panel_width']==420,saved.values['right_panel_width']
                        assert saved.values['right_panel_selected']=='panel-two:chat'
                        w.shutdown();w.destroy();app.quit();state['step']=9;return False
                    elif state['step']==6:
                        panel=host.panels['panel-one:chat'];panel.input.get_buffer().set_text('é'*9000)
                        state['step']=8
                    elif state['step']==8:
                        assert text(host.panels['panel-one:chat'].input)=='é'*9000  # Keep oversized drafts intact.
                        state['first'].ui.remove_panel('chat');state['step']=7
                    elif state['step']==7:
                        assert 'panel-one:chat' not in host.panels
                        w.plugins.deactivate('panel-one');w.plugins.deactivate('panel-two')
                        assert not host.get_visible()
                        assert w.editors[0].dirty and file.read_text()=='original\n'
                        w.shutdown();w.destroy();app.quit();state['step']=9;return False
                except Exception as error:
                    import traceback;traceback.print_exc();errors.append(str(error));w.shutdown();w.destroy();app.quit();return False
                return True
            source=GLib.timeout_add(120,tick);app.run(['naui-panels-smoke'])
            if GLib.MainContext.default().find_source_by_id(source):GLib.source_remove(source)
            if errors:raise AssertionError(errors)
            assert state['step']==9,state
        print('PASS: public right panels, owner isolation, guarded input, draft preservation, focus, resize/presets/restart, unload, intact editor and PTY')
    return 0
if __name__=='__main__':raise SystemExit(main())

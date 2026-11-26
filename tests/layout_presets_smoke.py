#!/usr/bin/env python3
"""Actual GTK geometry, preset actions and restart recovery; disposable state."""
import os
from pathlib import Path
import tempfile
import time
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]: gi.require_version(name,version)
from gi.repository import Gtk,Gdk,GLib
from naui.gui import NauiApplication
from naui.ui_settings import UISettings

def main():
    Gtk.init()
    if Gdk.Display.get_default() is None: return 77
    with tempfile.TemporaryDirectory(prefix='naui-layout-presets-') as directory:
        root=Path(directory);os.environ.update(XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),SHELL='/bin/bash')
        source=root/'example.txt';source.write_text('keep this file\n')
        for n in range(20): (root/f'folder-{n}').mkdir()
        for run in range(2):
            app=NauiApplication([],directory);state={'step':0};errors=[];deadline=time.monotonic()+30
            def tick():
                w=app.window
                if w is None:return True
                try:
                    assert time.monotonic()<deadline,state
                    tree=w.workspace_sidebar.explorers['local']
                    if run==1:
                        if not w.editors or any(e.loading for e in w.editors) or tree.scroll.get_height()<300:return True
                        assert tree.scroll.get_height()==330,tree.scroll.get_height()
                        assert w.ui_settings.values['explorer_device_heights']['local']==330
                        assert w.notebook.paned and abs(w.notebook.paned.get_position()/w.notebook.get_width()-.6)<.07
                        assert w.editors[0].dirty and 'draft' in w.editors[0].text()
                        assert 'Review' in w.layout_presets.store.presets
                        w.layout_presets.store.delete('Review')
                        w.shutdown();w.destroy();app.quit();state['step']=9;return False
                    if state['step']==0 and w.at_prompt:
                        w.set_default_size(1600,900)
                        w.open_file(w.local_backend,str(source));state['step']=1
                    elif state['step']==1 and w.editors and not w.editors[0].loading:
                        state['editor']=w.editors[0];state['pty']=w.terminal.bridge.process
                        state['editor'].buffer.insert(state['editor'].buffer.get_end_iter(),'draft\n')
                        tree.resize_begin(None,0,0);tree.resize_origin=100;tree.resize_update(None,0,230)
                        w.sidebar_width=380;w.split.set_position(380)
                        w.split_tabs(Gtk.Orientation.HORIZONTAL);state['step']=2
                    elif state['step']==2:
                        assert tree.scroll.get_height()==330,tree.scroll.get_height()
                        w.notebook.paned.set_position(round(w.notebook.get_width()*.6))
                        state['step']=3
                    elif state['step']==3:
                        w.layout_presets.name_dialog()
                        entry=w.dialog.get_content_area().get_first_child().get_next_sibling()
                        entry.set_text('Coding');w.dialog.response(Gtk.ResponseType.ACCEPT)
                        assert 'Coding' in w.layout_presets.store.presets
                        w.layout_presets.name_dialog('Coding')
                        entry=w.dialog.get_content_area().get_first_child().get_next_sibling()
                        entry.set_text('Review');w.dialog.response(Gtk.ResponseType.ACCEPT)
                        w.notebook.single_view();tree.set_tree_size(80)
                        w.layout_presets.apply('Review');state['step']=4
                    elif state['step']==4:
                        assert tree.scroll.get_height()==330,tree.scroll.get_height()
                        assert abs(w.notebook.paned.get_position()/w.notebook.get_width()-.6)<.07
                        assert w.editors[0] is state['editor'] and w.editors[0].dirty
                        assert state['pty'].poll() is None
                        w.save_ui();saved=UISettings(w.ui_settings.path)
                        assert saved.valid and saved.values['explorer_device_heights']['local']==330
                        assert abs(saved.values['split_ratio']-600)<70,saved.values['split_ratio']
                        w.shutdown();w.destroy();app.quit();state['step']=9;return False
                except Exception as error:
                    import traceback;traceback.print_exc();errors.append(str(error));w.shutdown();w.destroy();app.quit();return False
                return True
            GLib.timeout_add(120,tick);app.run(['naui-layout-presets-smoke'])
            if errors:raise AssertionError(errors)
            assert state['step']==9,state
        print('PASS: Explorer resizing, save/rename/apply/delete presets, split ratio and height after restart, dirty buffer and live PTY preserved')
    return 0
if __name__=='__main__':raise SystemExit(main())

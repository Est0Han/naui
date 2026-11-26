#!/usr/bin/env python3
"""Desktop search acceptance. Exit 77 without a display, never claim a pass."""
import gc
import os
from pathlib import Path
import sys
import tempfile
import time
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('Vte','3.91'),('GtkSource','5')]:gi.require_version(name,version)
from gi.repository import Gdk,Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no display; Search rendering/editor/sidebar lifecycle acceptance not tested.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-search-smoke-') as directory:
        base=Path(directory);repo=base/'example-project';repo.mkdir()
        (base/'config').mkdir();(base/'cache').mkdir()
        os.environ.update(XDG_CONFIG_HOME=str(base/'config'),XDG_CACHE_HOME=str(base/'cache'),SHELL='/bin/bash')
        (repo/'src').mkdir();(repo/'tests').mkdir()
        source=repo/'src/device.py';source.write_text('def connect():\n    return "connect"\n# connect\n')
        (repo/'tests/test_device.py').write_text('print("connect")\n')
        app=NauiApplication([],str(repo));sources=Sources();failures=[]
        state={'stage':'start','switches':0};deadline=time.monotonic()+50
        old_hook=sys.excepthook
        def hook(kind,error,trace): failures.append(error);old_hook(kind,error,trace)
        sys.excepthook=hook
        def tick():
            try:
                window=app.window
                if failures:raise failures[0]
                assert time.monotonic()<deadline,state
                panel=window.search_panel
                if state['stage']=='start' and window.at_prompt:
                    panel.focus_search('content');panel.entry.set_text('connect');panel.search();state['stage']='content'
                elif state['stage']=='content' and panel.session and panel.session.state=='completed' and panel.count==4:
                    assert panel.tree.get_n_items()>2
                    assert panel.scroll.get_policy()[0]==Gtk.PolicyType.NEVER
                    window.show_sidebar_panel('explorer');window.show_sidebar_panel('search')
                    assert panel.count==4
                    position = next(index for index in range(panel.tree.get_n_items())
                        if not panel.tree.get_row(index).get_item().group and panel.tree.get_row(index).get_item().result.logical_path==str(source))
                    panel.open_result(position);state['stage']='editor'
                elif state['stage']=='editor' and window.current_editor() and not window.current_editor().loading:
                    editor=window.current_editor()
                    assert editor.device_id=='local' and editor.path==str(source)
                    assert editor.buffer.get_text(*editor.buffer.get_selection_bounds(),False)=='connect'
                    panel.focus_search('files');panel.entry.set_text('tdev');panel.search();state['stage']='files'
                elif state['stage']=='files' and panel.session and panel.session.state=='completed' and panel.count:
                    assert any(result.logical_path==str(repo/'tests/test_device.py') for result in panel.results)
                    panel.focus_search('content');panel.regex.set_active(True);panel.entry.set_text('[');panel.search()
                    assert 'Invalid regex' in panel.status.get_text()
                    panel.regex.set_active(False);panel.entry.set_text('connect');panel.search();state['stage']='stress'
                elif state['stage']=='stress':
                    for _ in range(10):
                        panel.search();panel.cancel();window.show_sidebar_panel('explorer');window.show_sidebar_panel('source_control');window.show_sidebar_panel('search')
                    state['switches']+=10;gc.collect()
                    if state['switches']>=50:
                        panel.clear();assert panel.store.get_n_items()==0
                        window.close();sources.close();state['stage']='done';return
            except Exception as error:
                failures.append(error);sources.close();app.quit();return
            sources.timeout(50,tick)
        app.connect('activate',lambda *_:sources.timeout(50,tick));code=app.run(['naui-search-smoke'])
        sys.excepthook=old_hook;sources.close()
        if failures:raise failures[0]
        assert state['stage']=='done',state
        assert code==0,code
        print('PASS: content/file/fuzzy/regex/navigation, preserved panels, 50 cancel/switch/GC cycles; exit 0')
        return 0


if __name__=='__main__':raise SystemExit(main())

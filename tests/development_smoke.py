#!/usr/bin/env python3
"""Real GTK recovery, task review, tab controls and replacement undo acceptance."""
import os
from pathlib import Path
import tempfile
import time
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]: gi.require_version(name,version)
from gi.repository import Gtk,Gdk,GLib
from naui.gui import NauiApplication
from naui.replacement import plan_replacements,review
from naui.search_model import SearchQuery

def main():
    Gtk.init()
    if Gdk.Display.get_default() is None: return 77
    with tempfile.TemporaryDirectory(prefix='naui-development-smoke-') as directory:
        root=Path(directory);os.environ.update(XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),SHELL='/bin/bash')
        a,b=root/'first.txt',root/'second.txt';a.write_text('first original\n');b.write_text('second original\n')
        app=NauiApplication([],directory);state={'step':0};errors=[];deadline=time.monotonic()+40
        def tick():
            w=app.window
            if w is None: return True
            try:
                assert time.monotonic()<deadline,state
                step=state['step']
                if step==0 and w.at_prompt:
                    w.open_file(w.local_backend,str(a),preview=False);w.open_file(w.local_backend,str(b),preview=False);state['step']=1
                elif step==1 and len(w.editors)==2 and all(not e.loading for e in w.editors):
                    first,second=w.editors
                    first.buffer.insert(first.buffer.get_end_iter(),'unsaved draft\n')
                    first.buffer.place_cursor(first.buffer.get_iter_at_offset(4))
                    w.notebook.set_pinned(first,True)
                    assert not first.tab_close_button.get_visible()
                    w.notebook.set_current_page(w.notebook.page_num(second));w.notebook.split(Gtk.Orientation.VERTICAL)
                    w.session_restore.save()
                    saved=w.session_restore.path.read_text()
                    assert 'unsaved draft' in saved
                    for editor in list(w.editors):
                        editor.buffer.set_modified(False);w.close_editor(editor)
                    w.session_restore.restore();state['step']=2
                elif step==2 and len(w.editors)==2 and all(not e.loading for e in w.editors):
                    first=next(e for e in w.editors if e.path==str(a));second=next(e for e in w.editors if e.path==str(b))
                    assert first.dirty and 'unsaved draft' in first.text()
                    assert first.buffer.get_iter_at_mark(first.buffer.get_insert()).get_offset()==4
                    assert first in w.notebook.pinned and len(w.notebook.groups)==2
                    assert a.read_text()=='first original\n'
                    w.notebook.set_current_page(w.notebook.page_num(first));w.notebook.set_current_page(w.notebook.page_num(second));w.notebook.recent_tab()
                    assert w.current_editor() is first
                    w.development.palette();assert w.dialog is not None;w.dialog.response(Gtk.ResponseType.CANCEL)
                    plan=plan_replacements([('local',w.local_backend,0,str(b))],SearchQuery('original'),'updated')
                    review(w,plan);assert w.dialog is not None
                    w.dialog.response(Gtk.ResponseType.ACCEPT)
                    assert second.text()=='second updated\n' and second.dirty
                    assert b.read_text()=='second original\n'
                    second.buffer.undo();assert second.text()=='second original\n'
                    w.close_editor(second);w.development.reopen();state['step']=3
                elif step==3 and len(w.editors)==2 and all(not e.loading for e in w.editors):
                    w.development.tasks=[dict(name='Smoke Task',device='local',cwd=directory,argv=['touch',str(root/'task-ran')])]
                    w.development.run_task();w.dialog.response(Gtk.ResponseType.ACCEPT);state['step']=4
                elif step==4 and w.dialog:
                    # Run remains explicit; it cannot execute before review approval.
                    assert not (root/'task-ran').exists()
                    w.dialog.response(Gtk.ResponseType.ACCEPT);state['step']=5
                elif step==5 and (root/'task-ran').exists():
                    w.shutdown();w.destroy();app.quit();return False
            except Exception as error:
                import traceback;traceback.print_exc();errors.append(str(error));w.shutdown();w.destroy();app.quit();return False
            return True
        GLib.timeout_add(50,tick);app.run(['naui-development-smoke'])
        if errors: raise AssertionError(errors)
        assert state['step']==5,state
        print('PASS: draft and split restore, private recovery, pinned/recent/reopened tabs, palette, replacement preview and undo, reviewed task execution')
    return 0
if __name__=='__main__':raise SystemExit(main())

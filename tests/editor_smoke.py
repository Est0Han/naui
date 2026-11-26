#!/usr/bin/env python3
"""Desktop-only GtkSource editor, search and mixed-tab lifecycle acceptance.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/editor_smoke.py
Only generated temporary files/configuration are used. No SSH host is contacted.
"""
import gc
import os
from pathlib import Path
import sys
import tempfile
import time

import gi
for name,version in (('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')): gi.require_version(name,version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources

CONTENT = 'def one():\n    print("one")\n\ndef two():\n    print("two")\n\ndef three():\n    print("three")\n'


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; rendered editor and mixed-tab lifecycle not tested.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-editor-smoke-') as directory:
        root=Path(directory)
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),SHELL='/bin/bash')
        path=root/'main.py';path.write_text(CONTENT)
        app=NauiApplication([str(path)],str(root))
        sources=Sources()
        state={'phase':'load','error':None}
        deadline=time.monotonic()+60
        def select(editor,start,end):
            editor.buffer.select_range(editor.buffer.get_iter_at_offset(end),editor.buffer.get_iter_at_offset(start))
        def tick():
            window=app.window
            try:
                assert time.monotonic()<deadline,'Editor smoke timeout: '+state['phase']
                if state['phase']=='load' and window.editors and not window.editors[0].loading:
                    editor=state['editor']=window.editors[0]
                    assert editor.document.language=='python3'
                    assert editor.view.get_show_line_numbers()
                    assert editor.buffer.get_highlight_matching_brackets()
                    assert editor.view.get_insert_spaces_instead_of_tabs()
                    editor.command('reveal')
                    select(editor,0,len(CONTENT))
                    assert editor.command('toggle_comment')
                    assert '# def one()' in editor.text()
                    assert editor.buffer.get_has_selection()
                    assert editor.command('toggle_comment')
                    assert editor.text()==CONTENT
                    editor.command('indent');editor.command('outdent')
                    assert editor.text()==CONTENT
                    end=CONTENT.index('def two')
                    select(editor,0,end)
                    editor.command('move_down');editor.command('move_up')
                    assert editor.text()==CONTENT
                    editor.command('duplicate');editor.buffer.undo()
                    assert editor.text()==CONTENT
                    editor.command('delete_line');editor.buffer.undo()
                    assert editor.text()==CONTENT
                    editor.buffer.redo();editor.buffer.undo()
                    assert editor.text()==CONTENT
                    editor.editing.show_search(True)
                    editor.editing.entry.set_text('print')
                    editor.editing.find_next()
                    state['phase']='search'
                elif state['phase']=='search':
                    editor=state['editor'];bounds=editor.buffer.get_selection_bounds()
                    if not bounds or editor.buffer.get_text(*bounds,True)!='print':
                        sources.timeout(30,tick);return
                    editor.editing.replacement.set_text('log')
                    editor.editing.replace_all()
                    assert editor.text()==CONTENT.replace('print','log')
                    editor.buffer.undo()
                    assert editor.text()==CONTENT
                    editor.editing.goto_line()
                    dialog=next(iter(editor.editing.dialogs))
                    dialog.get_content_area().get_first_child().set_value(6)
                    dialog.response(Gtk.ResponseType.ACCEPT)
                    assert editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_line()==5
                    editor.buffer.insert_at_cursor('# editor smoke\n')
                    editor.save()
                    state['phase']='save'
                elif state['phase']=='save' and not state['editor'].saving:
                    editor=state['editor']
                    assert path.read_text()==editor.text()
                    assert not editor.dirty
                    window.new_terminal()
                    identities=list(window.terminals)
                    assert len(identities)>=2
                    window.close_terminal(identities[-1],force=True)
                    # Start a native async search immediately before closing the
                    # editor, exercising cancellation before widget removal.
                    editor.editing.entry.set_text('def')
                    editor.editing.find_next()
                    window.close_editor(editor)
                    editor.close()
                    gc.collect()
                    state['phase']='cycles';state['cycles']=0
                elif state['phase']=='cycles':
                    # Real widgets/signal groups/context menus are constructed
                    # and removed repeatedly without a second editor engine.
                    editor=window.open_snapshot(window.local_backend,window.local_backend.read(str(path)))
                    assert editor.document.device_id=='local'
                    editor.editing.entry.set_text('def');editor.editing.find_next()
                    window.close_editor(editor)
                    gc.collect()
                    state['cycles']+=1
                    if state['cycles']==20:
                        sources.close()
                        window.close()
                        return
            except BaseException as error:
                state['error']=error
                sources.close()
                if window:
                    window._discard_approved=True
                    window.close()
                return
            sources.timeout(30,tick)
        sources.timeout(50,tick)
        result=app.run([])
        sources.close()
        if state['error']: raise state['error']
        assert state['phase']=='cycles' and state['cycles']==20
        assert result==0
        print('PASS: comments, indentation, moves, duplicate/delete, undo/redo, async search, replace, go-to-line, save; 20 editor closes with GC; mixed terminal close; exit 0')
        return 0


if __name__=='__main__':raise SystemExit(main())

#!/usr/bin/env python3
"""Optional real-Pyright completion acceptance, with no user project/config."""
import os
from pathlib import Path
import shutil
import tempfile
import time
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]: gi.require_version(name,version)
from gi.repository import Gtk,Gdk,GLib
from naui.gui import NauiApplication

def main():
    Gtk.init()
    if Gdk.Display.get_default() is None or not shutil.which('pyright-langserver'): return 77
    with tempfile.TemporaryDirectory(prefix='naui-assist-smoke-') as directory:
        root=Path(directory);os.environ.update(XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),SHELL='/bin/bash')
        source=root/'example.py';source.write_text('value = "hello"\nvalue.up\n')
        app=NauiApplication([str(source)],directory);state={'step':0};errors=[];deadline=time.monotonic()+45
        def children(widget):
            yield widget
            child=widget.get_first_child()
            while child:
                yield from children(child);child=child.get_next_sibling()
        def tick():
            w=app.window
            if w is None: return True
            try:
                assert time.monotonic()<deadline,(state,[(e.code_status.get_text(), e.text()) for e in w.editors])
                if not w.editors: return True
                e=w.editors[0];document=w.code_navigation.documents[e]
                if state['step']==0 and not e.loading and document.session and document.session.client:
                    e.buffer.place_cursor(e.buffer.get_iter_at_offset(len(e.text())-1))
                    w.code_navigation.assist(e,'completion');state['step']=1
                elif state['step']==1 and w.dialog:
                    entry=next(x for x in children(w.dialog) if isinstance(x,Gtk.SearchEntry))
                    entry.set_text('upper');state['step']=2;state['at']=time.monotonic()
                elif state['step']==2 and time.monotonic()-state['at']>.4:
                    state['choices']=[x.get_text() for x in children(w.dialog) if isinstance(x,Gtk.Label)]
                    w.dialog.response(Gtk.ResponseType.ACCEPT);state['step']=3
                elif state['step']==3 and 'value.upper' in e.text():
                    assert e.dirty and source.read_text()=='value = "hello"\nvalue.up\n'
                    e.buffer.undo();assert e.text()=='value = "hello"\nvalue.up\n'
                    w.shutdown();w.destroy();app.quit();return False
            except Exception as error:
                import traceback;traceback.print_exc();errors.append(str(error));w.shutdown();w.destroy();app.quit();return False
            return True
        GLib.timeout_add(50,tick);app.run(['naui-assist-smoke'])
        if errors: raise AssertionError(errors)
        assert state['step']==3
        print('PASS: real Pyright completion, keyboard filter/select, undo, no implicit save')
    return 0
if __name__=='__main__': raise SystemExit(main())

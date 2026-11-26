"""Native GTK generic binary preview smoke test."""
import os
import tempfile
import time
from pathlib import Path
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]:
    gi.require_version(name,version)
from gi.repository import GLib
from naui.gui import NauiApplication
def fixture(): return b"\x00\xffbinary preview text\x00"


def main():
    with tempfile.TemporaryDirectory() as directory:
        os.environ.update(XDG_CONFIG_HOME=directory+'/config',XDG_CACHE_HOME=directory+'/cache')
        paths=[Path(directory)/'test.bin',Path(directory)/'second.bin']
        for path in paths: path.write_bytes(fixture())
        app=NauiApplication([],directory);state={'step':0};errors=[];deadline=time.monotonic()+25
        def tick():
            w=app.window
            try:
                assert time.monotonic()<deadline
                if state['step']==0 and w.at_prompt:
                    w.open_path(w.local_backend,str(paths[0]));state['step']=1
                elif state['step'] in (1,2) and w.viewers and not w.viewers[-1].loading:
                    viewer=w.viewers[-1]
                    assert viewer.pages.get_pages().get_n_items()==4
                    for title in ('Overview','Text','Strings','Raw bytes'):
                        view=viewer.pages.get_child_by_name(title).get_child()
                        assert not view.get_editable()
                        assert view.get_buffer().get_char_count()>0
                    if state['step']==1:
                        w.open_path(w.local_backend,str(paths[1]));state['step']=2
                    else:
                        assert viewer.path.endswith('.bin')
                        for path in paths: assert path.read_bytes()==fixture()
                        w.close_viewer(viewer)
                        w.shutdown();w.destroy();app.quit();state['step']=3;return False
            except Exception as exc:
                import traceback;traceback.print_exc();errors.append(str(exc));w.shutdown();w.destroy();app.quit();return False
            return True
        app.connect_after('activate',lambda *_:GLib.timeout_add(100,tick));app.run(['binary-smoke'])
        assert not errors,errors
        assert state['step']==3,state
        print('PASS: BIN routing, four read-only tabs, preview replacement, unchanged binaries, close')

if __name__=='__main__':main()

"""Long host sizing and local/SSH choice with real widgets; no network calls."""
import os
import tempfile
import time
import gi
for name,version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]:gi.require_version(name,version)
from gi.repository import Gtk, GLib, Pango
from naui.gui import NauiApplication
with tempfile.TemporaryDirectory() as directory:
    os.environ.update(XDG_CONFIG_HOME=directory+'/config',XDG_CACHE_HOME=directory+'/cache')
    app=NauiApplication([],directory);errors=[];done=[];deadline=time.monotonic()+20
    def tick():
        w=app.window
        if w is None or not w.at_prompt:
            assert time.monotonic()<deadline
            return True
        try:
            remote=w.workspace.add_ssh('pi@'+'a'*100+'.onion')
            w.workspace_sidebar.add_device(remote)
            w.workspace.active_device_id=remote.id
            for _ in range(3): w.update_workspace_ui()
            button=w.workspace_sidebar.cards[remote.id][2]
            assert button.get_child().get_ellipsize()==Pango.EllipsizeMode.END
            assert button.measure(Gtk.Orientation.HORIZONTAL,-1)[0]<200
            w.set_status('SSH connecting: '+remote.display_name)
            assert w.status.measure(Gtk.Orientation.HORIZONTAL,-1)[0]<200
            w.new_terminal();assert w.dialog is not None
            count=len(w.terminals)
            w.dialog.response(1)
            assert len(w.terminals)==count+1
            assert w.runtimes[w.active_terminal_id].device_id=='local'
            w.workspace.active_device_id=remote.id
            called=[];w.connect_device=lambda device,new_terminal=False:called.append((device.id,new_terminal))
            w.new_terminal();w.dialog.response(2)
            assert called==[(remote.id,True)]
            w.new_terminal();w.dialog.response(Gtk.ResponseType.CANCEL)
            assert len(called)==1
            done.append(True)
        except Exception as error:
            import traceback;traceback.print_exc();errors.append(str(error))
        w.shutdown();w.destroy();app.quit();return False
    app.connect_after('activate',lambda *_:GLib.timeout_add(100,tick))
    app.run(['terminal-choice-smoke'])
    assert done and not errors,errors
print('PASS: refreshed long SSH labels stay compact; local, SSH and cancel choices route correctly')

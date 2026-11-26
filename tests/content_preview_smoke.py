"""Shared preview policy across files, diffs, text, browsers and settings."""
import os,tempfile,time
from pathlib import Path
import gi
for n,v in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]:gi.require_version(n,v)
from gi.repository import Gtk,GLib
from naui.gui import NauiApplication

PATCH='--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n'
def main():
 with tempfile.TemporaryDirectory(prefix='naui-content-preview-') as d:
  root=Path(d);file=root/'file.txt';file.write_text('original\n')
  os.environ.update(XDG_CONFIG_HOME=d+'/config',XDG_CACHE_HOME=d+'/cache',SHELL='/bin/bash')
  app=NauiApplication([],d);state={'step':0};errors=[];deadline=time.monotonic()+30
  def tick():
   w=app.window;t=w.notebook
   try:
    assert time.monotonic()<deadline,state
    if state['step']==0 and w.at_prompt:
     state['pty']=w.terminal.bridge.process
     state['file']=w.open_file(w.local_backend,str(file));state['step']=1
    elif state['step']==1 and not state['file'].loading:
     state['diff1']=w.open_diff_view('local','First diff',PATCH);state['step']=2
    elif state['step']==2:
     assert state['file'] not in w.editors
     assert state['diff1'] in t.previews and state['diff1'].label.get_attributes() is not None
     state['diff2']=w.open_diff_view('local','Second diff',PATCH);state['step']=3
    elif state['step']==3:
     assert state['diff1']._shutting_down and state['diff1'] not in w.viewers
     tab=t.owner(state['diff2']).get_tab_label(state['diff2']);controllers=tab.observe_controllers()
     for i in range(controllers.get_n_items()):
      c=controllers.get_item(i)
      if isinstance(c,Gtk.GestureClick) and c.get_button()==1:c.emit('pressed',2,1.,1.)
     assert state['diff2'] not in t.previews and state['diff2'] not in t.pinned
     state['text']=w.open_text_view('local','Text output','Result',None);state['step']=4
    elif state['step']==4:
     assert state['diff2'] in w.viewers
     state['browser']=w.open_browser_view('local','Browser');state['step']=5
    elif state['step']==5:
     assert state['text']._shutting_down
     browser=state['browser']
     browser.set_detail('Click file',[{'text':'Open file','select':lambda:w.open_file(w.local_backend,str(file))}])
     browser.files.get_first_child().emit('clicked')
     assert not browser._shutting_down,'Must not dispose the sender during its native callback'
     state['step']=6
    elif state['step']==6 and w.editors and not w.editors[0].loading:
     assert state['browser']._shutting_down
     editor=w.editors[0];editor.buffer.insert(editor.buffer.get_end_iter(),'unsaved\n');state['dirty']=editor
     w.show_keybindings();state['settings']=t.current;state['step']=7
    elif state['step']==7:
     assert state['dirty'].dirty and state['dirty'] in w.editors
     assert state['settings'] in t.previews
     w.show_plugins();state['manager']=w.plugin_manager_view;state['step']=8
    elif state['step']==8:
     assert state['settings']._shutting_down
     assert state['manager'] in t.previews
     state['manager'].busy=True
     state['last']=w.open_diff_view('local','Final diff',PATCH);state['step']=9
    elif state['step']==9:
     assert not state['manager']._shutting_down and state['manager'] not in t.previews
     state['manager'].busy=False
     t.set_pinned(state['last'],True)
     w.open_text_view('local','Last text','Finished',None);state['step']=10
    elif state['step']==10:
     assert state['last'] in w.viewers and state['last'] in t.pinned
     assert state['pty'].poll() is None
     assert file.read_text()=='original\n' and state['dirty'].dirty
     w.shutdown();w.destroy();app.quit();state['step']=11;return False
   except Exception as e:
    import traceback;traceback.print_exc();errors.append(str(e));w.shutdown();w.destroy();app.quit();return False
   return True
  app.connect_after('activate',lambda *_:GLib.timeout_add(150,tick));app.run(['content-preview'])
  assert not errors,errors
  assert state['step']==11,state
  print('PASS: mixed content replacement, italic diff, kept/pinned views, native callback teardown, busy manager and dirty editor protection, live PTY')
if __name__=='__main__':main()

"""Native tab menus: targeted save/close, dirty snapshots and split actions."""
import os,tempfile,time
from pathlib import Path
import gi
for n,v in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]:gi.require_version(n,v)
from gi.repository import Gtk,GLib
from naui.gui import NauiApplication

def main():
 with tempfile.TemporaryDirectory(prefix='naui-tab-menu-') as d:
  root=Path(d);a=root/'a.py';b=root/'b.py';a.write_text('a\n');b.write_text('b\n')
  os.environ.update(XDG_CONFIG_HOME=d+'/config',XDG_CACHE_HOME=d+'/cache',SHELL='/bin/bash')
  app=NauiApplication([],d);state={'step':0};errors=[];deadline=time.monotonic()+30
  def click(page,title):
   w=app.window;t=w.notebook;tab=t.owner(page).get_tab_label(page)
   controllers=tab.observe_controllers()
   next(controllers.get_item(i) for i in range(controllers.get_n_items()) if isinstance(controllers.get_item(i),Gtk.GestureClick) and controllers.get_item(i).get_button()==3).emit('pressed',1,3.,3.)
   button=w.tab_menu.popover.get_child().get_first_child()
   while button and (not isinstance(button,Gtk.Button) or button.get_label()!=title):button=button.get_next_sibling()
   assert button and button.get_sensitive(),title
   button.emit('clicked')
  def tick():
   w=app.window;t=w.notebook
   try:
    assert time.monotonic()<deadline,state
    if state['step']==0 and w.at_prompt:
     state['a']=w.open_file(w.local_backend,str(a),preview=False)
     state['b']=w.open_file(w.local_backend,str(b),preview=False);state['step']=1
    elif state['step']==1 and all(not e.loading for e in w.editors):
     state['a'].buffer.insert(state['a'].buffer.get_end_iter(),'saved\n')
     click(state['a'],'Save');state['step']=2
    elif state['step']==2 and not state['a'].dirty and not state['a'].saving:
     assert a.read_text()=='a\nsaved\n' and b.read_text()=='b\n'
     state['a'].buffer.insert(state['a'].buffer.get_end_iter(),'draft\n')
     click(state['a'],'Duplicate Tab as Snapshot');state['step']=3
    elif state['step']==3 and t.current not in w.editors:
     state['copy']=t.current
     buffer=t.current.buffer
     assert 'draft' in buffer.get_text(buffer.get_start_iter(),buffer.get_end_iter(),True)
     assert not t.current.view.get_editable() and state['a'].dirty
     click(state['a'],'Split Side by Side');state['step']=4
    elif state['step']==4 and t.paned:
     assert t.current is state['a']
     click(state['copy'],'Split Top and Bottom');state['step']=5
    elif state['step']==5 and t.paned.get_orientation()==Gtk.Orientation.VERTICAL:
     click(state['copy'],'Pin Tab');state['step']=6
    elif state['step']==6 and state['copy'] in t.pinned:
     click(state['copy'],'Unpin Tab');state['step']=7
    elif state['step']==7 and state['copy'] not in t.pinned:
     click(state['a'],'Close Tab');state['step']=8
    elif state['step']==8 and w.dialog:
     w.dialog.response(Gtk.ResponseType.CANCEL);state['step']=9
    elif state['step']==9 and not w.dialog:
     assert state['a'] in w.editors and state['a'].dirty
     click(state['copy'],'Close Tab');state['step']=10
    elif state['step']==10 and state['copy'] not in w.viewers:
     # Also test application shutdown while a menu is still parented to a tab.
     w.tab_menu.show(t.owner(state['a']).get_tab_label(state['a']),state['a'],1,1)
     w.shutdown();w.destroy();app.quit();state['step']=11;return False
   except Exception as e:
    import traceback;traceback.print_exc();errors.append(str(e));w.shutdown();w.destroy();app.quit();return False
   return True
  app.connect_after('activate',lambda *_:GLib.timeout_add(150,tick));app.run(['menu-test'])
  assert not errors,errors
  assert state['step']==11,state
  print('PASS: actual right-click menus, background-tab save, unsaved snapshot, split layouts, pin/unpin, discard cancellation and popup teardown')
if __name__=='__main__':main()

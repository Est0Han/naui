"""Real preview replacement, keep-open, pinning and dirty-buffer protection."""
import os, tempfile, time
from pathlib import Path
from types import SimpleNamespace
import gi
for n,v in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')]:gi.require_version(n,v)
from gi.repository import Gtk,GLib
from naui.gui import NauiApplication

def main():
 with tempfile.TemporaryDirectory(prefix='naui-preview-') as d:
  root=Path(d)
  os.environ.update(XDG_CONFIG_HOME=d+'/config',XDG_CACHE_HOME=d+'/cache',SHELL='/bin/bash')
  for name in ('a','b','c','d','e','f','g','h'):(root/name).write_text(name+'\n')
  app=NauiApplication([],d);state={'step':0};errors=[];deadline=time.monotonic()+25
  def tick():
   w=app.window;t=w.notebook
   try:
    assert time.monotonic()<deadline,state
    if state['step']==0 and w.at_prompt:
     state['a']=w.open_file(w.local_backend,str(root/'a'));state['step']=1
    elif state['step']==1 and not state['a'].loading:
     assert state['a'] in t.previews
     assert state['a'].label.get_attributes() is not None
     state['b']=w.open_file(w.local_backend,str(root/'b'));state['step']=2
    elif state['step']==2 and not state['b'].loading:
     assert state['a'] not in w.editors
     b=state['b'];tab=t.owner(b).get_tab_label(b)
     controllers=tab.observe_controllers()
     for i in range(controllers.get_n_items()):
      controller=controllers.get_item(i)
      if isinstance(controller,Gtk.GestureClick) and controller.get_button()==1:controller.emit('pressed',2,1.,1.)
     assert b not in t.previews and b not in t.pinned
     assert b.label.get_attributes() is None and not hasattr(tab,'pin_indicator')
     state['c']=w.open_file(w.local_backend,str(root/'c'));state['step']=3
    elif state['step']==3 and not state['c'].loading:
     c=state['c'];t.set_pinned(c,True)
     assert c not in t.previews
     t.set_pinned(c,False)
     assert not t.owner(c).get_tab_label(c).pin_indicator.get_visible()
     state['d']=w.open_file(w.local_backend,str(root/'d'));state['step']=4
    elif state['step']==4 and not state['d'].loading:
     d=state['d'];d.buffer.insert(d.buffer.get_end_iter(),'unsaved\n')
     assert d not in t.previews
     state['e']=w.open_file(w.local_backend,str(root/'e'));state['step']=5
    elif state['step']==5 and not state['e'].loading:
     assert all(state[name] in w.editors for name in ('b','c','d','e'))
     assert state['d'].dirty
     assert t.split(Gtk.Orientation.HORIZONTAL)
     # Moving into a split explicitly keeps that file; each group then gets its own preview.
     state['f']=w.open_file(w.local_backend,str(root/'f'))
     t.set_current_page(t.page_num(state['b']))
     state['a2']=w.open_file(w.local_backend,str(root/'a'));state['step']=6
    elif state['step']==6 and not state['f'].loading and not state['a2'].loading:
     assert state['f'] in t.previews and state['a2'] in t.previews
     assert t.owner(state['f']) is not t.owner(state['a2'])
     assert (root/'d').read_text()=='d\n'
     pending=[];work=w.work
     w.work=lambda action,callback:pending.append(callback)
     try:
      w.open_path(w.local_backend,str(root/'g'))
      w.open_path(w.local_backend,str(root/'h'),preview=False)
     finally:w.work=work
     pending[0](SimpleNamespace(sample=b'text'),None)
     assert not any(e.path==str(root/'g') for e in w.editors)
     pending[1](SimpleNamespace(sample=b'text'),None)
     state['step']=7
    elif state['step']==7 and all(not e.loading for e in w.editors):
     h=next(e for e in w.editors if e.path==str(root/'h'))
     assert h not in t.previews
     w.shutdown();w.destroy();app.quit();state['step']=8;return False
   except Exception as e:
    import traceback;traceback.print_exc();errors.append(str(e));w.shutdown();w.destroy();app.quit();return False
   return True
  app.connect_after('activate',lambda *_:GLib.timeout_add(120,tick));app.run(['preview-test'])
  assert not errors,errors
  assert state['step']==8,state
  print('PASS: preview replacement, italic labels, double-click keep, pin/unpin icons, dirty protection, independent split previews')
if __name__=='__main__':main()

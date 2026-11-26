"""Generic toolbar popup/draft checks; no provider processes or network."""
import gi
for name,version in [('Gtk','4.0'),('GtkSource','5')]:gi.require_version(name,version)
from gi.repository import Gtk,GLib,Gdk,Graphene
from naui.plugin_panels import PluginPanel
app=Gtk.Application(application_id='test.naui.toolbars');state={};errors=[]
def tick():
 try:
  panel=state['panel'];step=state.get('step',0)
  if step==0:
   assert isinstance(panel.bottom_toolbar,Gtk.Box)
   assert isinstance(panel.bottom_toolbar.controls['usage']['button'],Gtk.Label)
   assert panel.bottom_toolbar.controls['usage']['button'].get_tooltip_text()=='5 hours: 65% remaining'
   assert panel.bottom_toolbar.controls['settings']['button'].get_icon_name()=='emblem-system-symbolic'
   panel.bottom_toolbar.controls['model']['button'].popup();state['step']=1
  elif step==1:
   button=panel.bottom_toolbar.controls['model']['button'];popover=button.get_popover()
   assert popover and popover.get_visible()
   panel.input.get_buffer().set_text('Unsent draft')
   panel.update('Chat',[], '',lambda _:None,'Message…',[{'id':'a','role':'assistant','text':'Streaming text'}],True,header_rows=state['header'],composer_rows=state['footer'])
   assert button.get_popover() is popover and popover.get_visible()
   assert panel.get_width()<=410,panel.get_width()
   # Find the model choice without relying on GTK row hierarchy.
   def walk(w):
    yield w
    child=w.get_first_child()
    while child:
     yield from walk(child);child=child.get_next_sibling()
   target=next(w for w in walk(popover) if isinstance(w,Gtk.Button) and not isinstance(w,Gtk.MenuButton))
   target.emit('clicked');state['step']=2
  elif step==2:
   paint=Gtk.WidgetPaintable.new(panel);snapshot=Gtk.Snapshot();paint.snapshot(snapshot,panel.get_width(),panel.get_height())
   node=snapshot.to_node()
   if node:
    texture=state['window'].get_renderer().render_texture(node,None);texture.save_to_png('/tmp/naui-chat-toolbar.png')
   assert state.get('selected')
   buf=panel.input.get_buffer();assert buf.get_text(buf.get_start_iter(),buf.get_end_iter(),True)=='Unsent draft'
   panel.close();state['window'].destroy();app.quit();return False
 except Exception as e:
  import traceback;traceback.print_exc();errors.append(str(e));app.quit();return False
 return True
def activate(app):
 w=Gtk.ApplicationWindow(application=app,default_width=390,default_height=760)
 panel=PluginPanel('AI',lambda:True)
 header=[{'id':'provider','text':'Claude','menu':[('Claude',lambda:None),('Codex',lambda:None)]},{'text':'History','children':[{'text':'Recent chat'}]},{'text':'New chat','select':lambda:None}]
 footer=[{'id':'model','text':'Default model · Default effort','children':[{'text':'Sonnet','select':lambda:state.update(selected=True)},{'text':'Opus','select':lambda:None}]},{'id':'usage','text':'65%','tooltip':'5 hours: 65% remaining'},{'id':'settings','text':'Settings','icon':'emblem-system-symbolic','children':[{'text':'Workspace'}]}]
 panel.update('AI',[], '',lambda _:None,'Message Claude…',[{'id':'a','role':'assistant','text':'What would you like to work on?'}],True,header_rows=header,composer_rows=footer)
 w.set_child(panel);w.present();state.update(panel=panel,window=w,header=header,footer=footer)
 GLib.timeout_add(300,tick)
app.connect('activate',activate);app.run([])
assert not errors,errors
print('PASS toolbar popup, streaming preservation, draft and narrow width')

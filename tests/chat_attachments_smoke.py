"""Real GTK clipboard and attachment composer checks without any provider calls."""
import base64
from pathlib import Path
import tempfile
import time
import gi
for name, version in [('Gtk','4.0'), ('Gdk','4.0')]: gi.require_version(name, version)
from gi.repository import Gtk, Gdk, GLib, Gio, GObject
from naui.plugin_panels import PluginPanel

def wait(predicate):
    deadline = time.monotonic()+5
    while not predicate():
        assert time.monotonic()<deadline, 'GUI callback timed out'
        GLib.MainContext.default().iteration(False)
        time.sleep(.005)

Gtk.init()
received=[]
panel=PluginPanel('Attachments', lambda: True)
window=Gtk.Window();window.set_child(panel);window.present()
panel.update('Attachments', [], '', received.append, 'Message', [], True)
with tempfile.TemporaryDirectory() as directory:
    file=Path(directory)/'example.txt';file.write_text('exact contents\n')
    panel.drop_files(None, Gdk.FileList.new_from_list([Gio.File.new_for_path(str(file))]), 0, 0)
    assert panel.attachment_list.get_visible()
    assert Path(panel.attachments[0]['path']).read_bytes()==file.read_bytes()
    panel.submit();wait(lambda: bool(received))
    assert received[-1]['text']=='' and len(received[-1]['attachments'])==1
    assert not panel.attachments
    image=Path(directory)/'image.png'
    import cairo
    cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 2).write_to_png(str(image))
    texture=Gdk.Texture.new_from_filename(str(image))
    panel.input.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.Texture, texture)))
    assert panel.paste_image(None, Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK)
    wait(lambda: not panel.attachment_pending)
    assert len(panel.attachments)==1, panel.error.get_text()
    assert Path(panel.attachments[0]['path']).read_bytes().startswith(b'\x89PNG')
    panel.remove_attachment(panel.attachments[0]);assert not panel.attachments
    long='é'*12000
    panel.input.get_buffer().set_text(long);panel.submit();wait(lambda: len(received)==2)
    assert Path(received[-1]['attachments'][0]['path']).read_text()==long
    panel.update('Attachments', [], '', lambda value: False, 'Message', [], True)
    panel.add_attachment(file);panel.input.get_buffer().set_text('keep me');panel.submit()
    wait(lambda: panel.input.get_buffer().get_char_count()>0)
    assert panel.attachments and panel.input.get_buffer().get_char_count()==7
    panel.update('Attachments', [], '', received.append, 'Message', [], False)
    panel.submit();assert panel.attachments and 'does not accept attachments' in panel.error.get_text()
    actions=[]
    card={'id':'approval', 'role':'notice', 'text':'Read file?', 'actions':[('Allow once', lambda: actions.append('allowed'))]}
    panel.conversation.update([card])
    panel.conversation.activate('approval', 0)
    panel.conversation.update([])
    GLib.MainContext.default().iteration(False)
    assert not actions, 'Removed approval must not dispatch'
    panel.conversation.update([card]);panel.conversation.activate('approval', 0)
    wait(lambda: bool(actions));assert actions==['allowed']
    staged=Path(panel.attachment_store.directory.name)
    panel.close();assert not staged.exists()
window.destroy()
print('Attachment clipboard, drop, long text, rejection and cleanup checks passed.')

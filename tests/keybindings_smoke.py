#!/usr/bin/env python3
"""Desktop-only rebinding/capture, plugin reload and terminal-safety acceptance.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/keybindings_smoke.py
Uses temporary config/plugins, never contacts SSH or an AI provider.
"""
import gc
import os
from pathlib import Path
import sys
import tempfile
import time
import gi
for name,version in (('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5'),('Vte','3.91')):gi.require_version(name,version)
from gi.repository import Gtk,Gdk
from naui.gui import NauiApplication
from naui.lifecycle import Sources
from naui.keybindings import Conflict


def children(widget):
    yield widget
    child=widget.get_first_child()
    while child:
        yield from children(child)
        child=child.get_next_sibling()


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; keybinding capture/UI and native terminal acceptance not tested.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-keybindings-smoke-') as directory:
        root=Path(directory)
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),SHELL='/bin/bash')
        path=root/'main.py';path.write_text('one()\ntwo()\n')
        app=NauiApplication([str(path)],str(root));sources=Sources()
        state={'phase':'load','error':None};deadline=time.monotonic()+60
        def tick():
            window=app.window
            try:
                assert time.monotonic()<deadline,'Keybinding smoke timeout'
                if state['phase']=='load' and window.editors and not window.editors[0].loading:
                    editor=state['editor']=window.editors[0]
                    window.notebook.set_current_page(window.notebook.page_num(editor));editor.view.grab_focus()
                    state['phase']='editor'
                elif state['phase']=='editor':
                    editor=state['editor'];registry=window.command_registry
                    editor.buffer.select_range(editor.buffer.get_end_iter(),editor.buffer.get_start_iter())
                    assert window.key_pressed(None,Gdk.KEY_slash,0,Gdk.ModifierType.CONTROL_MASK)
                    assert editor.text()=='# one()\n# two()\n'
                    registry.set('editor.toggle_comment','F6')
                    assert not window.key_pressed(None,Gdk.KEY_slash,0,Gdk.ModifierType.CONTROL_MASK)
                    assert window.key_pressed(None,Gdk.KEY_F6,0,0)
                    assert editor.text()=='one()\ntwo()\n'
                    registry.reset('editor.toggle_comment')
                    before=dict(registry.overrides)
                    try:registry.set('editor.toggle_comment','Ctrl+G')
                    except Conflict:pass
                    else:raise AssertionError('Missing conflict')
                    assert registry.overrides==before
                    window.show_keybindings();settings=window.viewers[-1]
                    settings.search.set_text('comment');settings.refresh()
                    settings.capture('editor.toggle_comment')
                    dialog=next(iter(settings.dialogs))
                    controllers=dialog.observe_controllers()
                    for index in range(controllers.get_n_items()):
                        controller=controllers.get_item(index)
                        if isinstance(controller,Gtk.EventControllerKey):controller.emit('key-pressed',Gdk.KEY_F6,0,0)
                    save=next(widget for widget in children(dialog) if isinstance(widget,Gtk.Button) and widget.get_label()=='Save')
                    save.emit('clicked')
                    assert registry.binding('editor.toggle_comment')=='F6'
                    profile=root/'profile.json';registry.export(profile);registry.reset_all();registry.import_profile(profile)
                    assert registry.binding('editor.toggle_comment')=='F6'
                    window.close_viewer(settings)
                    plugin=root/'test-plugin';plugin.mkdir()
                    (plugin/'plugin.toml').write_text('id="test-plugin"\nname="Test Plugin"\nversion="0.1.0"\napi_version=1\nentrypoint="plugin.py"\n[permissions]\ncommands=true\n')
                    (plugin/'plugin.py').write_text('from naui_plugin_api import Plugin\nclass Plugin(Plugin):\n def activate(self, api):\n  api.commands.register("hello", "Hello", lambda: api.notifications.show("Hello"), default_shortcut="F7")\n def deactivate(self): pass\n')
                    for index in range(20):
                        window.plugins.load_user(plugin,{'commands'})
                        registry.set('test-plugin.hello','F8')
                        assert registry.dispatch('F8','global')
                        window.plugins.deactivate('test-plugin');window.plugins.deactivate('test-plugin')
                        assert registry.resolve('F8','global') is None
                        gc.collect()
                    terminal=window.terminal
                    window.notebook.set_current_page(window.notebook.page_num(terminal));terminal.view.grab_focus()
                    state['phase']='terminal'
                elif state['phase']=='terminal':
                    assert not window.key_pressed(None,Gdk.KEY_d,0,Gdk.ModifierType.CONTROL_MASK)
                    assert not window.key_pressed(None,Gdk.KEY_c,0,Gdk.ModifierType.CONTROL_MASK)
                    assert not window.key_pressed(None,Gdk.KEY_z,0,Gdk.ModifierType.CONTROL_MASK)
                    print('PASS: runtime comment rebind/reset, conflict cancellation, capture UI, profile roundtrip, 20 plugin enable/disable/GC cycles and PTY control-key pass-through.')
                    sources.close();window._discard_approved=True;window.close();return
            except BaseException as error:
                state['error']=str(error);sources.close();window._discard_approved=True;window.close();return
            sources.timeout(25,tick)
        sources.timeout(25,tick);code=app.run([sys.argv[0]])
        if state['error']:print('FAIL:',state['error']);return 1
        return code


if __name__=='__main__':raise SystemExit(main())

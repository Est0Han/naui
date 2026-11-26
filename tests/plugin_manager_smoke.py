#!/usr/bin/env python3
"""Desktop acceptance: AI scaffold, validation, ten reloads, ZIP and teardown.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings
python3 tests/plugin_manager_smoke.py
Uses disposable config/cache and keeps the desktop runtime directory intact.
"""
import gc
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import zipfile
import gi

for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('Vte', '3.91'), ('GtkSource', '5')]:
    gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources

CODE = '''from naui_plugin_api import Plugin as BasePlugin
class Plugin(BasePlugin):
    def __init__(self): self.api = None
    def activate(self, api):
        self.api = api
        api.commands.add('hello', 'Hello Test', lambda: api.notifications.show('revision-VERSION'))
        api.ui.sidebar('info', 'HELLO TEST', [{'text': 'Plugin-owned panel'}])
        api.ui.open_text('local', 'Hello Output', 'Read-only text')
        self.browser = api.ui.open_browser('local', 'Hello Browser', lambda: None, lambda: None)
        self.browser.rows([{'text': 'Example', 'select': lambda: None}])
        api.defer(60000, lambda: api.notifications.show('Late callback'))
    def deactivate(self): self.api = None
'''


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from descendants(child)
        child = child.get_next_sibling()


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; Plugin Manager GUI acceptance was not run.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-plugins-smoke-') as directory:
        root = Path(directory)
        for name in ('config', 'cache'): (root/name).mkdir(mode=0o700)
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'), XDG_CACHE_HOME=str(root/'cache'), SHELL='/bin/bash')
        app, sources, errors = NauiApplication([], str(root)), Sources(), []
        state = dict(stage='start', revision=0)
        deadline = time.monotonic() + 180
        old_hook = sys.excepthook
        def exception(kind, value, traceback):
            errors.append(value); old_hook(kind, value, traceback)
        sys.excepthook = exception
        def accept(window):
            assert window.dialog is not None
            window.dialog.response(Gtk.ResponseType.ACCEPT)
        def tick():
            window = app.window
            try:
                if errors: raise errors[0]
                assert time.monotonic() < deadline, state
                assert not window._shutting_down
                stage = state['stage']
                if stage == 'start':
                    window.show_plugins()
                    view = state['view'] = window.plugin_manager_view
                    # Keep the manager open across this scripted batch of reloads.
                    window.notebook.keep_open(view)
                    assert any(r.spec.bundled for r in view.catalog.records.values())
                    view.create_dialog()
                    widgets = list(descendants(window.dialog.get_content_area()))
                    entries = [w for w in widgets if isinstance(w, Gtk.Entry)]
                    entries[0].set_text('Hello Test'); entries[1].set_text('hello-test')
                    next(w for w in widgets if isinstance(w, Gtk.TextView)).get_buffer().set_text('Add a command that displays a Naui notification.')
                    for widget in widgets:
                        if isinstance(widget, Gtk.CheckButton) and widget.get_label() in ('Register commands / dialogs', 'Add panels and read-only views'):
                            widget.set_active(True)
                    accept(window); state['stage'] = 'scaffold'
                else:
                    view = state['view']
                    plugin = view.catalog.store.directory/'hello-test'
                    if stage == 'scaffold' and plugin.is_dir() and window.dialog:
                        assert str(plugin) in view.last_prompt
                        assert 'PLUGIN_API_CAPABILITY_MISSING: <description>' in view.last_prompt
                        assert 'DO NOT modify Naui core' in view.last_prompt
                        assert 'AGENT_INSTRUCTIONS.md' in view.last_prompt
                        view.copy(view.last_prompt)
                        window.dialog.response(Gtk.ResponseType.CANCEL)
                        (plugin/'plugin.py').write_text(CODE.replace('VERSION', '0'))
                        # The replacement fixture needs the API, unlike the empty scaffold.
                        test = plugin/'tests/test_plugin.py'
                        test.write_text(test.read_text().replace('plugin.activate(object())', 'plugin.activate(__import__("unittest.mock", fromlist=["Mock"]).Mock())'))
                        view.toggle('hello-test'); accept(window); state['stage'] = 'enabled'
                    elif stage == 'enabled' and not view.busy and 'hello-test' in window.plugins.plugins:
                        revision = state['revision']
                        window.plugins.invoke('hello-test', 'hello')
                        assert window.status.get_text() == 'revision-' + str(revision), (revision, window.status.get_text(), list(window.plugins.logs['hello-test']))
                        api = window.plugins.apis['hello-test']
                        assert len(api._views) == 2
                        assert api._views[-1] in window.viewers
                        assert all(v in window.viewers or v._shutting_down for v in api._views)
                        assert len([key for key in window.sidebar_sections if key[0] == 'hello-test']) == 1
                        gc.collect()
                        if revision < 10:
                            state['revision'] += 1
                            (plugin/'plugin.py').write_text(CODE.replace('VERSION', str(state['revision'])))
                            view.reload('hello-test'); accept(window)
                            state['stage'] = 'reloaded'
                        else:
                            (plugin/'plugin.py').write_text(CODE.replace('VERSION', '10') + '\nimport naui.gui\n')
                            view.validate('hello-test'); accept(window); state['stage'] = 'invalid'
                    elif stage == 'reloaded' and not view.busy and window.dialog is None:
                        state['stage'] = 'enabled'
                    elif stage == 'invalid' and not view.busy and window.dialog is None:
                        assert not view.catalog.records['hello-test'].validation.ok
                        view.copy_error('hello-test')
                        assert 'Forbidden private/native import: naui.gui' in view.last_prompt
                        assert 'hello-test' in window.plugins.plugins  # Invalid edits never unload working code.
                        (plugin/'plugin.py').write_text(CODE.replace('VERSION', '10'))
                        archive = root/'plugin.zip'
                        with zipfile.ZipFile(archive, 'w') as output:
                            for path in plugin.rglob('*'):
                                if path.is_file():
                                    content = path.read_bytes()
                                    if path.name == 'plugin.toml': content = content.replace(b'hello-test', b'zip-test')
                                    output.writestr('zip-test/'+path.relative_to(plugin).as_posix(), content)
                        view.prepare(str(archive), True); state['stage'] = 'zip-review'
                    elif stage == 'zip-review' and not view.busy and window.dialog:
                        assert 'zip-test' not in window.plugins.plugins
                        accept(window); state['stage'] = 'zip-installed'
                    elif stage == 'zip-installed' and 'zip-test' in view.catalog.records:
                        assert 'zip-test' not in window.plugins.plugins
                        view.toggle('zip-test'); accept(window); state['stage'] = 'zip-enabled'
                    elif stage == 'zip-enabled' and not view.busy and 'zip-test' in window.plugins.plugins:
                        owned = list(window.plugins.apis['zip-test']._views)
                        view.toggle('zip-test')
                        assert all(v not in window.viewers for v in owned)
                        view.uninstall('zip-test'); accept(window); state['stage'] = 'zip-removed'
                    elif stage == 'zip-removed' and 'zip-test' not in view.catalog.records:
                        archive = root/'evil.zip'
                        with zipfile.ZipFile(archive, 'w') as output: output.writestr('../../evil.txt', 'evil')
                        view.prepare(str(archive), True); state['stage'] = 'zip-rejected'
                    elif stage == 'zip-rejected' and not view.busy:
                        assert 'Unsafe ZIP' in view.status.get_text()
                        owned = list(window.plugins.apis['hello-test']._views)
                        view.toggle('hello-test')
                        assert all(v not in window.viewers for v in owned)
                        assert not any(key[0] == 'hello-test' for key in window.plugins.commands)
                        assert not any(key[0] == 'hello-test' for key in window.sidebar_sections)
                        gc.collect()
                        marker = root/'terminal-alive'
                        state['marker'] = marker
                        next(iter(window.terminals.values())).send_input(('printf alive > ' + shlex.quote(str(marker)) + '\n').encode())
                        state['stage'] = 'terminal'
                    elif stage == 'terminal' and state['marker'].exists():
                        assert state['marker'].read_text() == 'alive'
                        # Close the application with a manager-owned dialog
                        # open: exactly one owner disposes native dialog objects.
                        view.text_dialog('Close with prompt', view.last_prompt, prompt=True)
                        state['stage'] = 'done'; sources.close(); window.close()
            except Exception as error:
                errors.append(error); sources.close()
                window._discard_approved = True; window.close(); return
            if state['stage'] != 'done': sources.timeout(50, tick)
        handler = app.connect_after('activate', lambda *_: sources.timeout(50, tick))
        try: result = app.run([sys.argv[0]])
        finally:
            sources.close(); app.disconnect(handler); sys.excepthook = old_hook
        if errors: raise errors[0]
        assert state['stage'] == 'done' and result == 0, (state, result)
        del state, app
        gc.collect()
        print('PASS: AI scaffold/prompt, validation/private-import error, ten native reloads/GC, '
              'ZIP install/rejection/uninstall, survivor terminal, dialog cleanup, normal exit 0')
    return 0


if __name__ == '__main__': sys.exit(main())

#!/usr/bin/env python3
"""Desktop activity switching/resize/scale and mixed-tab lifecycle acceptance.

Uses a disposable repository, a disconnected mock SSH device and mock transfer
snapshots. Never connects to a server. Run with G_DEBUG=fatal-warnings.
"""
import gc
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import gi
for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('GtkSource', '5'), ('Vte', '3.91')]: gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.commands import local_command
from naui.lifecycle import Sources
from naui.transfers import FileLocation
from naui.ui_settings import UISettings


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from descendants(child)
        child = child.get_next_sibling()


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; rendered activity/width/zoom/lifecycle acceptance not run.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-activity-smoke-') as directory:
        root = Path(directory); repo = root / 'project'; repo.mkdir()
        # Preserve the desktop runtime: do not break an already-open Wayland connection.
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'), XDG_CACHE_HOME=str(root/'cache'), SHELL='/bin/bash')
        def git(*args): return local_command(('git', *args), str(repo))
        git('init', '-q', '-b', 'main'); git('config', 'user.name', 'Naui Test'); git('config', 'user.email', 'test@example.invalid')
        for index in range(12):
            folder = repo / ('directory-' + str(index)); folder.mkdir()
            (folder/('long-file-name-' * 8 + '.py')).write_text('original=1\n')
        git('add', '-A'); git('commit', '-qm', 'Fixture')
        for file in repo.rglob('*.py'): file.write_text('changed=2\n')
        app = NauiApplication([], str(repo)); sources = Sources()
        state = {'phase': 'ready', 'cycle': 0}; errors, retained = [], []
        deadline = time.monotonic() + 180
        original_hook = sys.excepthook
        def exception(kind, value, trace):
            errors.append(value); original_hook(kind, value, trace)
        sys.excepthook = exception
        def check_scrolls(panel):
            for widget in descendants(panel):
                if isinstance(widget, Gtk.ScrolledWindow):
                    assert widget.get_policy()[0] == Gtk.PolicyType.NEVER
                    horizontal = widget.get_hadjustment()
                    assert horizontal.get_value() == 0
                    assert horizontal.get_upper() - horizontal.get_page_size() <= 1
        def tick():
            window = app.window
            try:
                if errors: raise errors[0]
                assert time.monotonic() < deadline, state
                assert not window._shutting_down
                rail = window.left_sidebar
                plugin = window.plugins.plugins['base-git'][1]
                if state['phase'] == 'ready' and window.at_prompt and rail.state.badges.get('source_control') == (12, False):
                    retained.append(window)
                    window.set_default_size(1500, 850)
                    state['tree'] = window.explorer.tree_model
                    state['scroll'] = window.explorer.scroll.get_vadjustment()
                    state['remote'] = window.workspace.add_ssh('example.invalid', 'Example Server')
                    window.workspace_sidebar.add_device(state['remote'])
                    assert len(window.workspace_sidebar.explorers) == 2
                    window.explorer.edit_path()
                    assert window.explorer.path_stack.get_visible_child_name() == 'entry'
                    window.explorer.path_entry.set_text('/not-a-navigation-request')
                    window.explorer.path_key_pressed(None, Gdk.KEY_Escape, 0, 0)
                    assert window.explorer.path_stack.get_visible_child_name() == 'breadcrumb'
                    assert window.explorer.path_entry.get_text() == str(repo)
                    state['snapshot'] = window.transfers.status
                    snapshots = []
                    for index, status in enumerate(('running', 'queued', 'failed')):
                        snapshots.append(dict(id='mock-' + str(index), state=status,
                            source={'device_id': 'local', 'path': str(repo/'sample.tar')},
                            destination={'device_id': state['remote'].id, 'path': '/opt/sample.tar'},
                            size=10000, bytes_transferred=3400, speed=1000, total_known=True,
                            files_completed=0, skipped=0, eta=6, paused=False,
                            error='Mock transfer error' if status == 'failed' else ''))
                    window.transfers.status = lambda: snapshots
                    window.sources.remove(window.transfer_panel.timer)
                    window.transfer_panel.poll()
                    assert rail.state.badges['transfers'] == (2, True)
                    for identity in ('search', 'settings', 'devices', 'explorer'): rail.select(identity)
                    state['layouts'] = [(width, scale) for width in (220, 300, 500, 700) for scale in (80, 100, 125, 150)]
                    state['phase'] = 'layout'
                elif state['phase'] == 'layout':
                    if state['layouts']:
                        state['width'], scale = state['layouts'].pop(0)
                        rail.scale(scale); window.split.set_position(state['width'])
                        state['phase'] = 'measure'
                    else:
                        rail.scale(100)
                        window.transfers.status = state['snapshot']
                        state['phase'] = 'switch'
                elif state['phase'] == 'measure':
                    if abs(window.left_sidebar.get_width() - state['width']) > 2: return sources.timeout(30, tick)
                    check_scrolls(window.workspace_sidebar)
                    assert window.ui_settings.values['editor_font'] == window.ui_settings.values['terminal_font'] == 12
                    for identity in ('source_control', 'transfers', 'devices', 'explorer'):
                        rail.select(identity)
                        assert rail.stack.get_visible_child() is rail.widgets[identity]
                    check_scrolls(rail.stack.get_visible_child())
                    state['phase'] = 'layout'
                elif state['phase'] == 'switch':
                    assert window.explorer.tree_model is state['tree']
                    assert window.explorer.scroll.get_vadjustment() is state['scroll']
                    repository = next(iter(plugin.repositories.values()))
                    plugin.diff(repository, repository.status.changes[0])
                    rail.select('source_control')
                    state['phase'] = 'diff'
                elif state['phase'] == 'diff' and window.viewers:
                    state['diff'] = window.viewers[-1]
                    rail.select('explorer')
                    assert window.notebook.page_num(state['diff']) >= 0
                    window.open_file(window.local_backend, next(repo.rglob('*.py')).as_posix())
                    state['phase'] = 'editor'
                elif state['phase'] == 'editor' and window.editors and not window.editors[-1].loading:
                    window.close_viewer(state['diff']); window.close_editor(window.editors[-1])
                    state['phase'] = 'create-terminal'
                elif state['phase'] == 'create-terminal':
                    local = window.workspace.devices['local']
                    state['survivor'] = next(iter(window.terminals))
                    window.create_terminal(local, str(repo)); state['closing'] = window.runtime.id
                    state['phase'] = 'close-terminal'
                elif state['phase'] == 'close-terminal' and window.at_prompt:
                    window.terminals[state['closing']].tab_close_button.emit('clicked')
                    state['phase'] = 'removed-terminal'
                elif state['phase'] == 'removed-terminal' and state['closing'] not in window.terminals:
                    for identity in ('source_control', 'transfers', 'devices', 'explorer'):
                        rail.select(identity)
                    window.toggle_sidebar(); window.toggle_sidebar()
                    gc.collect()
                    assert state['survivor'] in window.terminals
                    assert state['closing'] not in window.terminals
                    assert window.workspace.devices[state['remote'].id].state == 'disconnected'
                    marker = root / ('survivor-' + str(state['cycle']))
                    state['marker'] = marker
                    survivor = window.terminals[state['survivor']]
                    survivor.send_input(('printf ok > ' + shlex.quote(str(marker)) + '\n').encode())
                    state['phase'] = 'verify-terminal'
                elif state['phase'] == 'verify-terminal' and state['marker'].exists():
                    assert state['marker'].read_text() == 'ok'
                    state['cycle'] += 1
                    if state['cycle'] < 50: state['phase'] = 'create-terminal'
                    else:
                        rail.select('source_control'); rail.scale(125); window.split.set_position(500); window.save_ui()
                        restored = UISettings(window.ui_settings.path)
                        assert restored.width == 500
                        assert restored.values['sidebar_panel'] == 'source_control'
                        assert restored.values['sidebar_scale'] == 125
                        state['phase'] = 'done'; sources.close(); window.close()
            except Exception as error:
                errors.append(error); sources.close()
                window._discard_approved = window._transfers_exit_approved = True
                window.close(); return
            if state['phase'] != 'done': sources.timeout(40, tick)
        handler = app.connect_after('activate', lambda *_: sources.timeout(40, tick))
        try: result = app.run([sys.argv[0]])
        finally:
            sources.close(); app.disconnect(handler); sys.excepthook = original_hook
        if errors: raise errors[0]
        assert state['phase'] == 'done' and result == 0
        assert retained[0]._shutting_down
        assert retained[0].left_sidebar.closed
        retained.clear(); del app; gc.collect()
        print('PASS: activity/retained tree, 220/300/500/700px, 80/100/125/150%, badges, diff/editor, 50 terminal close/GC/survivor-command cycles, layout restore, exit 0')
    return 0


if __name__ == '__main__': raise SystemExit(main())

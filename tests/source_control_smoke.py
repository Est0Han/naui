#!/usr/bin/env python3
"""Desktop-only Source Control groups/actions/diffs and mixed-tab stress.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 -B tests/source_control_smoke.py
Uses disposable repositories/config; no SSH connection or network push.
"""
import gc
import os
from pathlib import Path
import sys
import tempfile
import time
import gi
for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('GtkSource', '5'), ('Vte', '3.91')]: gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.diff_view import DiffView
from naui.commands import local_command
from naui.plugins.git_service import GIT
from naui.lifecycle import Sources
from naui.transfers import FileLocation


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; rendered Source Control and mixed-tab stress were not run.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-source-control-smoke-') as directory:
        root = Path(directory); repo = root/'project'; repo.mkdir()
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'), XDG_CACHE_HOME=str(root/'cache'), SHELL='/bin/bash')
        def git(*argv): return local_command((*GIT, *argv), str(repo))
        git('init', '-q', '-b', 'main'); git('config', 'user.name', 'Naui Test'); git('config', 'user.email', 'test@example.invalid')
        for name in ('modified.py', 'deleted.py', 'staged.py'): (repo/name).write_text('original=1\n')
        git('add', '-A'); git('commit', '-qm', 'Initial fixture')
        (repo/'modified.py').write_text('changed=2\n'); (repo/'deleted.py').unlink()
        (repo/'staged.py').write_text('staged=3\n'); git('add', '--', 'staged.py')
        (repo/'untracked õ file.py').write_text('new=4')
        app = NauiApplication([], str(repo)); sources = Sources()
        state = {'phase': 'load', 'stress': 0}; errors = []; deadline = time.monotonic()+100
        def tick():
            window = app.window
            try:
                assert time.monotonic() < deadline, 'Source Control timeout: ' + state['phase']
                plugin = window.plugins.plugins.get('base-git', (None, None))[1]
                repository = plugin.repositories.get(('local', str(repo))) if plugin else None
                if not repository or not repository.status or repository.busy or repository.operation:
                    return sources.timeout(40, tick)
                phase = state['phase']
                changes = {c.path: c for c in repository.status.changes}
                if phase == 'load':
                    section = window.sidebar_sections[('base-git', 'status')]
                    assert section.scroll.get_policy()[0] == Gtk.PolicyType.NEVER
                    assert len(changes) == 4
                    window.split.set_position(180)
                    plugin.file_row(repository, changes['modified.py'], False)['select']()
                    state['phase'] = 'diff'
                elif phase == 'diff':
                    view = window.notebook.get_nth_page(window.notebook.get_current_page())
                    if not isinstance(view, DiffView): return sources.timeout(40, tick)
                    assert not view.view.get_editable() and not view.left.get_editable()
                    assert 'changed=2' in view.buffer.get_text(view.buffer.get_start_iter(), view.buffer.get_end_iter(), True)
                    assert 'original=1' in view.left_buffer.get_text(view.left_buffer.get_start_iter(), view.left_buffer.get_end_iter(), True)
                    assert view.diff_context['location'].device_id == 'local'
                    view.mode.set_selected(1)
                    assert '+ changed=2' in view.buffer.get_text(view.buffer.get_start_iter(), view.buffer.get_end_iter(), True)
                    assert window.command_registry.dispatch('F7', 'git')
                    view.mode.set_selected(0)
                    window.close_viewer(view); gc.collect()
                    plugin.mutate(repository, 'stage', changes['modified.py']); state['phase'] = 'staged'
                elif phase == 'staged' and changes.get('modified.py') and changes['modified.py'].index == 'M':
                    plugin.mutate(repository, 'unstage', changes['modified.py']); state['phase'] = 'unstaged'
                elif phase == 'unstaged' and changes.get('modified.py') and changes['modified.py'].index == '.':
                    repository.draft = 'Commit only staged'
                    plugin.mutate(repository, 'commit', message=repository.draft); state['phase'] = 'committed'
                elif phase == 'committed' and 'staged.py' not in changes:
                    assert repository.draft == ''
                    assert len(changes) == 3
                    window.create_terminal(window.workspace.devices['local'], str(repo))
                    window.create_terminal(window.workspace.devices['local'], str(repo))
                    window.terminals[list(window.terminals)[1]].tab_close_button.emit('clicked')
                    state['phase'] = 'terminal'
                elif phase == 'terminal' and len(window.terminals) == 2:
                    plugin.api.open(FileLocation('local', str(repo/'modified.py')))
                    state['phase'] = 'editor'
                elif phase == 'editor' and window.editors and not window.editors[-1].loading:
                    assert window.editors[-1].text() == 'changed=2\n'
                    state['phase'] = 'stress'
                elif phase == 'stress':
                    view = window.open_diff_view('local', 'Stress Diff', '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n')
                    assert isinstance(view, DiffView)
                    view.mode.set_selected(1); view.mode.set_selected(0); view.change(1)
                    window.close_viewer(view); view.close(); gc.collect()
                    assert len(window.terminals) == 2
                    state['stress'] += 1
                    if state['stress'] == 50:
                        state['phase'] = 'done'; sources.close(); window.close(); return
            except Exception as exc:
                errors.append(exc); sources.close(); window._discard_approved = True; window.close(); return
            sources.timeout(40, tick)
        handler = app.connect_after('activate', lambda *_: sources.timeout(40, tick))
        old_hook = sys.excepthook
        def failed(kind, error, traceback):
            errors.append(error); sources.close()
            if app.window: app.window._discard_approved = True; app.window.close()
        sys.excepthook = failed
        try: result = app.run([sys.argv[0]])
        finally: sources.close(); app.disconnect(handler); sys.excepthook = old_hook
        if errors: raise errors[0]
        assert result == 0 and state['phase'] == 'done', (result, state)
        gc.collect()
        print('PASS: groups, sidebar policy, side/inline diff, hunk command, stage/unstage, staged-only commit, editor, terminal close, 50 diff close/GC cycles, exit 0')
    return 0


if __name__ == '__main__': raise SystemExit(main())

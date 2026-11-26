#!/usr/bin/env python3
"""Desktop acceptance: left layout, live tree/editor, real runner, Git diff/exit."""
import os
from pathlib import Path
import sys
import tempfile
import time
import gi

for name, version in [('Gtk','4.0'),('Gdk','4.0'),('Vte','3.91'),('GtkSource','5')]:
    gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.commands import local_command
from naui.gui import NauiApplication
from naui.lifecycle import Sources
from naui.plugins.base_runner import file_command


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; rendered watcher/runner/Git acceptance not tested.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-watch-smoke-') as directory:
        root=Path(directory)
        for name in ('runtime','config','cache'): (root/name).mkdir(mode=0o700)
        os.environ.update(XDG_RUNTIME_DIR=str(root/'runtime'), XDG_CONFIG_HOME=str(root/'config'), XDG_CACHE_HOME=str(root/'cache'))
        repo=root/'repo';repo.mkdir()
        for argv in (['git','init','-q','-b','main'],['git','config','user.name','Naui Test'],
                     ['git','config','user.email','naui-test@example.invalid']):local_command(argv,str(repo))
        file=repo/'main.py'
        original='from pathlib import Path\nPath("ran.txt").write_text("RUN_OK")\n'
        file.write_text(original)
        local_command(['git','add','--','main.py'],str(repo));local_command(['git','commit','-qm','Initial'],str(repo))
        app=NauiApplication([],str(repo));sources=Sources()
        state={'stage':'ready','error':None};retained=[];deadline=time.monotonic()+50
        def paths(window):
            model=window.workspace_sidebar.explorers['local'].tree_model
            return {model.get_row(index).get_item().entry.path for index in range(model.get_n_items())} if model else set()
        def tick():
            window=app.window
            try:
                assert time.monotonic()<deadline,state['stage']
                plugin=window.plugins.plugins['base-git'][1]
                if state['stage']=='ready' and window.at_prompt and plugin.repositories:
                    assert window.split.get_start_child() is window.left_sidebar
                    assert window.split.get_end_child() is window.notebook
                    assert window.workspace_sidebar.get_parent() is window.left_sidebar.stack
                    assert window.left_sidebar.stack.get_visible_child() is window.workspace_sidebar
                    state['tree_model']=window.explorer.tree_model
                    (repo/'external.txt').write_text('external')
                    window.open_file(window.local_backend,str(file));state['stage']='tree'
                elif state['stage']=='tree' and str(repo/'external.txt') in paths(window) and not window.editors[0].loading:
                    assert window.explorer.tree_model is state['tree_model']
                    state['editor']=window.editors[0]
                    file.write_text(original+'# outside update\n');state['stage']='clean'
                elif state['stage']=='clean' and state['editor'].external.snapshot:
                    editor=state['editor'];assert editor.text()==original
                    editor.reload_external();editor.buffer.set_text('unsaved editor text\n')
                    file.write_text(original+'# outside dirty update\n');state['stage']='dirty'
                elif state['stage']=='dirty' and state['editor'].external.snapshot:
                    editor=state['editor'];assert editor.text()=='unsaved editor text\n'
                    editor.compare_external();assert window.notebook.get_nth_page(window.notebook.get_current_page()) is not editor
                    editor.reload_external();window.dialog.response(Gtk.ResponseType.REJECT)
                    state['stage']='run'
                elif state['stage']=='run' and window.at_prompt:
                    api=window.plugins.apis['base-runner']
                    api.terminals.run(device_id='local',cwd=str(repo),command=file_command(str(file),0o600))
                    state['stage']='ran'
                elif state['stage']=='ran' and (repo/'ran.txt').exists() and window.at_prompt:
                    assert (repo/'ran.txt').read_text()=='RUN_OK'
                    git_repo=plugin.repositories[('local',str(repo))]
                    if git_repo.status and any(change.path=='main.py' for change in git_repo.status.changes):
                        plugin.diff(git_repo,next(change for change in git_repo.status.changes if change.path=='main.py'))
                        state['stage']='diff'
                elif state['stage']=='diff':
                    page=window.notebook.get_nth_page(window.notebook.get_current_page())
                    if getattr(page,'path','').startswith('Diff:'):
                        assert page.device_id=='local' and not page.view.get_editable()
                        # Regression: ordinary file opens still work after a diff view.
                        window.open_path(window.local_backend,str(repo/'external.txt'))
                        state['stage']='reopen'
                elif state['stage']=='reopen' and len(window.editors)==2 and not window.editors[-1].loading:
                    sources.close();window.close();state['stage']='closed';return
                sources.timeout(30,tick)
            except Exception as error:
                state['error']=error;sources.close()
                window._discard_approved=window._transfers_exit_approved=True;window.close()
        def activated(application):
            retained.append(application.window);sources.timeout(30,tick)
        handler=app.connect_after('activate',activated)
        try:result=app.run([sys.argv[0]])
        finally:sources.close();app.disconnect(handler)
        if state['error']:raise state['error']
        assert result==0 and state['stage']=='closed'
        window=retained[0]
        assert window.file_watch.closed and not window.file_watch.callbacks and not window.file_watch.observers
        assert window.command_service.cancelled.is_set() and not window.sources._ids
        print('PASS: left sidebar, incremental native watch, clean/dirty conflicts, PTY runner, reactive Git/diff and clean exit')
        return 0


if __name__=='__main__':raise SystemExit(main())

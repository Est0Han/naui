#!/usr/bin/env python3
"""Desktop acceptance: narrow vertical-only sidebar, inline names and Git history."""
import gc
import os
from pathlib import Path
import sys
import tempfile
import time
import gi
for name, version in [('Gtk','4.0'),('Gdk','4.0'),('Vte','3.91'),('GtkSource','5')]:gi.require_version(name,version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.commands import local_command
from naui.lifecycle import Sources


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no display; rendered sidebar widths/inline scroll/history acceptance not tested.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-sidebar-smoke-') as directory:
        root=Path(directory)
        for name in ('config','cache','runtime'):(root/name).mkdir(mode=0o700)
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'),XDG_CACHE_HOME=str(root/'cache'),SHELL='/bin/bash')
        repo=root/('very-long-project-name-'*4);repo.mkdir()
        def git(*args):return local_command(('git',*args),str(repo))
        git('init','-q','-b','main');git('config','user.name','Naui Test');git('config','user.email','test@example.invalid')
        file=repo/('long-file-name-'*10+'.py');file.write_text('first=1\n')
        git('add','--',file.name);git('commit','-qm','Initial fixture\n\nFull message')
        git('branch','feature/history')
        file.write_text('modified=2\n');(repo/'untracked.txt').write_text('new')
        (repo/'staged.txt').write_text('staged');git('add','--','staged.txt')
        deep=repo/'folder'
        for index in range(12):deep=deep/('level-'+str(index))
        deep.mkdir(parents=True)
        app=NauiApplication([],str(repo));sources=Sources();retained=[];errors=[]
        state={'stage':'start','widths':[220,300,500,700]};deadline=time.monotonic()+70
        old_hook=sys.excepthook
        def hook(kind,value,traceback):errors.append(value);old_hook(kind,value,traceback)
        sys.excepthook=hook
        def checks(window):
            controls=[window.workspace_sidebar.scroll,window.explorer.scroll]
            controls += [section.scroll for section in window.sidebar_sections.values()]
            for scroll in controls:
                assert scroll.get_policy()[0] == Gtk.PolicyType.NEVER
                adjustment=scroll.get_hadjustment()
                assert adjustment.get_value()==0,adjustment.get_value()
                assert adjustment.get_upper()-adjustment.get_page_size()<=1,(adjustment.get_upper(),adjustment.get_page_size())
        def tick():
            window=app.window
            try:
                if errors:raise errors[0]
                assert time.monotonic()<deadline,state
                plugin=window.plugins.plugins['base-git'][1]
                if state['stage']=='start' and window.at_prompt and plugin.repositories and next(iter(plugin.repositories.values())).status:
                    retained.append(window)
                    assert 'base-git' in window.plugins.plugins
                    assert ('base-git','status') in window.sidebar_sections
                    status=next(iter(plugin.repositories.values())).status
                    assert status.branch=='main'
                    assert {'M','A','?'}.issubset({change.decoration for change in status.changes})
                    window.create_terminal(window.workspace.devices['local'],str(repo))
                    window.create_terminal(window.workspace.devices['local'],str(repo))
                    identities=list(window.terminals);window.terminals[identities[1]].tab_close_button.emit('clicked')
                    state['stage']='expand'
                elif state['stage']=='expand':
                    model=window.explorer.tree_model
                    for index in range(model.get_n_items()):
                        row=model.get_row(index)
                        if row.get_item().entry.is_directory:row.set_expanded(True)
                    if any(model.get_row(index).get_item().entry.path==str(deep) for index in range(model.get_n_items())):
                        assert len(window.terminals)==2
                        state['stage']='width'
                elif state['stage']=='width':
                    if state['widths']:
                        state['width']=state['widths'].pop(0);window.split.set_position(state['width']);state['stage']='measure'
                    else:
                        window.explorer.name_dialog('New File',str(repo),window.local_backend,None);state['stage']='inline'
                elif state['stage']=='measure':
                    if abs(window.left_sidebar.get_width()-state['width'])>2:return sources.timeout(30,tick)
                    checks(window);state['stage']='width'
                elif state['stage']=='inline' and window.explorer.inline_entry:
                    entry=window.explorer.inline_entry
                    assert entry.get_width_chars()==1
                    entry.set_text('new-long-file-'*10+'.py');entry.set_position(-1)
                    state['stage']='typed'
                elif state['stage']=='typed':
                    checks(window)
                    window.explorer.inline_entry.emit('activate');state['stage']='created'
                elif state['stage']=='created' and window.explorer.inline is None:
                    assert (repo/('new-long-file-'*10+'.py')).exists()
                    entry=window.local_backend.properties(str(file))
                    window.explorer.name_dialog('Rename',str(repo),window.local_backend,None,entry)
                    state['stage']='rename'
                elif state['stage']=='rename' and window.explorer.inline_entry:
                    window.explorer.inline_entry.set_text('renamed-long-file-'*8+'.py')
                    checks(window);window.explorer.inline_entry.emit('activate');state['stage']='renamed'
                elif state['stage']=='renamed' and window.explorer.inline is None:
                    assert (repo/('renamed-long-file-'*8+'.py')).exists()
                    checks(window);plugin.history();state['stage']='history'
                elif state['stage']=='history' and plugin.histories:
                    history=next(iter(plugin.histories.values()))
                    if history.busy or not history.commits:return sources.timeout(30,tick)
                    view=window.notebook.get_nth_page(window.notebook.get_current_page())
                    assert view.get_parent() is not window.left_sidebar
                    assert 'Git History' in view.label.get_text()
                    assert 'Full message' in view.buffer.get_text(view.buffer.get_start_iter(),view.buffer.get_end_iter(),True)
                    history.choose_filter('refs/heads/feature/history');state['stage']='filtered'
                elif state['stage']=='filtered':
                    history=next(iter(plugin.histories.values()))
                    if history.busy:return sources.timeout(30,tick)
                    history.select(history.commits[0]);state['stage']='detail'
                elif state['stage']=='detail':
                    view=window.notebook.get_nth_page(window.notebook.get_current_page())
                    if not view.files.get_first_child():return sources.timeout(30,tick)
                    view.files.get_first_child().emit('clicked');state['stage']='diff'
                elif state['stage']=='diff':
                    view=window.notebook.get_nth_page(window.notebook.get_current_page())
                    if not view.label.get_text().startswith('Commit:'):return sources.timeout(30,tick)
                    assert 'first=1' in view.buffer.get_text(view.buffer.get_start_iter(),view.buffer.get_end_iter(),True)
                    checks(window);state['stage']='done';sources.close();window.close()
            except Exception as error:
                if not errors:errors.append(error)
                sources.close();window._discard_approved=True;window.close();return
            if state['stage']!='done':sources.timeout(30,tick)
        handler=app.connect_after('activate',lambda *_:sources.timeout(30,tick))
        try:result=app.run([sys.argv[0]])
        finally:sources.close();app.disconnect(handler);sys.excepthook=old_hook
        if errors:raise errors[0]
        assert result==0 and state['stage']=='done',(result,state)
        retained.clear();del app;gc.collect()
        print('PASS: sidebar 220/300/500/700px, no horizontal range, inline creation/rename, history/details/diff, exit 0')
    return 0


if __name__=='__main__':raise SystemExit(main())

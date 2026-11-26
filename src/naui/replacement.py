"""Review workspace replacement before applying undoable, conflict-checked drafts."""
import difflib
import json
from pathlib import Path
import subprocess
import sys
from gi.repository import Gtk
from .diff_view import DiffView
from .lifecycle import Signals


def plan_replacements(targets, query, replacement):
    plan, total = [], 0
    for identity, backend, generation, path in targets:
        if getattr(backend, 'generation', 0) != generation: raise ValueError('Connection changed; search again')
        snapshot = backend.read(path)
        if snapshot.path != path or snapshot.revision == 'missing': raise ValueError('File identity changed; search again')
        payload = dict(text=snapshot.content, query=query.text, replacement=replacement,
                       regex=query.regex, case=query.case_sensitive, word=query.whole_word)
        try:
            result = subprocess.run([sys.executable, '-I', str(Path(__file__).with_name('replace_worker.py'))],
                input=json.dumps(payload), text=True, capture_output=True, timeout=2, check=True)
        except subprocess.TimeoutExpired as error: raise ValueError('Replacement pattern took too long; simplify it') from error
        data = json.loads(result.stdout)
        if data.get('error'): raise ValueError(data['error'])
        total += len(snapshot.content.encode()) + len(data['content'].encode())
        if total > 32*1024*1024: raise ValueError('Replacement preview exceeds 32 MiB; narrow your search')
        if data['content'] != snapshot.content:
            plan.append((identity,backend,generation,snapshot,data['content'],data['count']))
    return plan


def preview(panel):
    w, session = panel.window, panel.session
    if w.dialog: w.dialog.present(); return
    if not session or session.state != 'completed' or not session.events.empty() or session.limited or session.warnings or panel.warning_text:
        panel.status.set_text('Finish a complete search without warnings before replacing.'); return
    if session.query.mode != 'content' or panel.query() != session.query:
        panel.status.set_text('Run the content search again before replacing.'); return
    identities = sorted({(result.device_id,result.logical_path) for result in panel.results})
    if not identities or len(identities) > 64:
        panel.status.set_text('Replacement needs 1–64 matched files. Narrow the search scope.'); return
    targets = []
    for identity,path in identities:
        device = w.workspace.devices.get(identity)
        if device is None or not device.connected:
            panel.status.set_text('A matched device is disconnected; reconnect and search again.'); return
        if any(e.device_id == identity and e.path == path and (e.dirty or e.loading or e.saving) for e in w.editors):
            panel.status.set_text('Save affected open files and search again before replacing.'); return
        backend = device.filesystem_backend
        targets.append((identity,backend,getattr(backend,'generation',0),path))
    replacement = panel.replace_entry.get_text()
    query = session.query
    panel.status.set_text('Preparing replacement preview…')
    def done(plan,error):
        if panel.closed or w._shutting_down: return
        if error: panel.status.set_text(str(error)); return
        if not plan: panel.status.set_text('No replacement changes.'); return
        review(w,plan)
    w.work(lambda: plan_replacements(targets,query,replacement), done)


def apply_plan(w, plan):
    opened = {(e.device_id,e.path):e for e in w.editors}
    if len(w.viewers) + sum((identity,snapshot.path) not in opened for identity,_,_,snapshot,_,_ in plan) > 64:
        raise ValueError('Close tabs before applying this replacement (64-tab limit)')
    for identity,backend,generation,snapshot,content,count in plan:
        device = w.workspace.devices.get(identity)
        if not device or device.filesystem_backend is not backend or getattr(backend,'generation',0) != generation:
            raise ValueError('A device changed after preview; no edits applied')
        editor = opened.get((identity,snapshot.path))
        if editor and (editor.loading or editor.saving or editor.text() != snapshot.content):
            raise ValueError('An affected buffer changed after preview; no edits applied')
    for identity,backend,generation,snapshot,content,count in plan:
        editor = opened.get((identity,snapshot.path)) or w.open_snapshot(backend,snapshot,w.workspace.devices[identity].session_id)
        editor.buffer.begin_user_action()
        try:
            editor.buffer.delete(editor.buffer.get_start_iter(),editor.buffer.get_end_iter())
            editor.buffer.insert(editor.buffer.get_start_iter(),content)
        finally: editor.buffer.end_user_action()
    w.set_status('Replacement applied to unsaved buffers. Review, undo, or Save each file.')


def review(w,plan):
    if w.dialog: w.set_status('Close the current dialog and retry the replacement preview.'); return
    dialog=Gtk.Dialog(title='Review Workspace Replacement',transient_for=w,modal=True)
    dialog.set_default_size(1000,650)
    dialog.add_button('Cancel',Gtk.ResponseType.CANCEL); dialog.add_button('Apply to Unsaved Buffers',Gtk.ResponseType.ACCEPT)
    box=dialog.get_content_area(); box.set_spacing(6)
    box.append(Gtk.Label(label=f'{sum(item[5] for item in plan)} replacements in {len(plan)} files. Choose each file to review. Saving remains explicit.',xalign=0))
    choice=Gtk.ComboBoxText()
    for identity,_,_,snapshot,_,count in plan:
        choice.append_text(w.workspace.devices[identity].display_name+' · '+snapshot.path+f' ({count})')
    box.append(choice)
    error_label=Gtk.Label(xalign=0,wrap=True); box.append(error_label)
    signals=Signals(); views=[]
    def show(*_):
        if views:
            old=views.pop(); box.remove(old); old.close()
        identity,_,_,snapshot,content,_=plan[choice.get_active()]
        patch=''.join(difflib.unified_diff(snapshot.content.splitlines(keepends=True),content.splitlines(keepends=True),fromfile='a/'+snapshot.path,tofile='b/'+snapshot.path))
        view=DiffView(identity,w.workspace.devices[identity].display_name,'Replacement',patch,context={'historical':True})
        views.append(view); box.append(view)
    def response(_,answer):
        if answer == Gtk.ResponseType.ACCEPT:
            try: apply_plan(w,plan)
            except (ValueError,OSError) as error: error_label.set_text(str(error)); return
        signals.close()
        for view in views: view.close()
        w.dialog=None; dialog.destroy()
    signals.connect(choice,'changed',show); signals.connect(dialog,'response',response)
    w.dialog=dialog; choice.set_active(0); dialog.present()

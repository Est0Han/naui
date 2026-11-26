"""Private bounded editor recovery. Never restores processes or reconnects SSH."""
from .development import atomic_json, read_json
from .backends import FileSnapshot

MAX_BYTES = 32 * 1024 * 1024


class SessionRestore:
    def __init__(self, window):
        self.window = window
        self.path = window.workspace_store.path.with_name('session.json')
        self.last = None
        self.enabled = True
        self.unrestored = []

    @staticmethod
    def validate(data):
        if not isinstance(data,dict) or data.get('version') != 1 or not isinstance(data.get('files'),list) or len(data['files'])>64:
            raise ValueError('Invalid saved editor session')
        if data.get('layout') not in ('single','horizontal','vertical'): raise ValueError('Invalid saved split layout')
        total=0
        identities=set()
        for item in data['files']:
            if not isinstance(item,dict): raise ValueError('Invalid saved file')
            for key in ('device','path','revision'):
                if not isinstance(item.get(key),str): raise ValueError('Invalid saved file identity')
            if not item['path'].startswith('/') or '\0' in item['path'] or len(item['path'])>4096 or len(item['device'])>64 or len(item['revision'])>1024:
                raise ValueError('Invalid saved file identity')
            identity=(item['device'],item['path'])
            if identity in identities: raise ValueError('Duplicate saved file')
            identities.add(identity)
            if type(item.get('offset')) is not int or not 0<=item['offset']<=8*1024*1024: raise ValueError('Invalid cursor position')
            if type(item.get('group')) is not int or item['group'] not in (0,1): raise ValueError('Invalid file group')
            if type(item.get('dirty')) is not bool or type(item.get('pinned')) is not bool: raise ValueError('Invalid file state')
            text=item.get('text')
            if text is not None:
                if not isinstance(text,str) or '\0' in text or len(text.encode())>8*1024*1024: raise ValueError('Invalid recovery text')
                total+=len(text.encode())
            if item['dirty'] and text is None: raise ValueError('Missing recovery text')
        if total>MAX_BYTES: raise ValueError('Recovery session exceeds 32 MiB')

    def save(self, discard=False):
        if not self.enabled: return
        w=self.window; files=[]; total=0
        if any(editor.loading for editor in w.editors):
            raise ValueError('Wait for files to finish loading before saving the session')
        for editor in [page for page in w.notebook.pages if page in w.editors]:
            if editor.loading or editor.revision is None or (discard and editor.dirty): continue
            device = w.workspace.devices.get(editor.device_id)
            text=editor.text() if editor.dirty or device is None or device.type=='ssh' else None
            total+=len(text.encode()) if text is not None else 0
            if total>MAX_BYTES:
                raise ValueError('Session recovery exceeds 32 MiB; save files before closing.')
            files.append(dict(device=editor.device_id,path=editor.path,revision=editor.revision,text=text,
                dirty=editor.dirty,offset=editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset(),
                group=w.notebook.groups.index(w.notebook.owner(editor)),pinned=editor in w.notebook.pinned))
        present={(item['device'],item['path']) for item in files}
        files.extend(item for item in self.unrestored if (item['device'],item['path']) not in present)
        from gi.repository import Gtk
        layout='single' if not w.notebook.paned else 'horizontal' if w.notebook.paned.get_orientation()==Gtk.Orientation.HORIZONTAL else 'vertical'
        current=w.current_editor()
        data=dict(version=1, files=files, layout=layout,active=[current.device_id,current.path] if current else None)
        self.validate(data)
        if data!=self.last:
            atomic_json(self.path,data); self.last=data

    def tick(self):
        w=self.window
        if w._shutting_down: return
        try: self.save()
        except (ValueError,OSError) as error: w.set_status('Session recovery: '+str(error))
        w.sources.timeout(5000,self.tick)

    def restore(self):
        w=self.window
        try:
            data=read_json(self.path,MAX_BYTES*6+65536,dict(version=1,files=[],layout='single'))
            self.validate(data)
        except (ValueError,OSError,TypeError) as error:
            self.enabled=False
            w.set_status('Saved session preserved but could not be restored: '+str(error)); return
        from gi.repository import Gtk
        restored=[]
        self.unrestored=[]
        for item in data['files']:
            device=w.workspace.devices.get(item['device'])
            if device is None:
                self.unrestored.append(item); continue
            existing=next((e for e in w.editors if e.device_id==device.id and e.path==item['path']),None)
            if existing: continue
            if item.get('text') is not None:
                editor=w.open_snapshot(device.filesystem_backend,FileSnapshot(item['path'],item['text'],item['revision']),device.session_id)
                if editor:
                    editor.buffer.set_modified(item['dirty'])
                    if not device.connected: editor.mark_disconnected('Restored offline. Reconnect this device explicitly to save or refresh.')
            elif device.connected: editor=w.open_file(device.filesystem_backend,item['path'],device.session_id,preview=False)
            else:
                self.unrestored.append(item); continue
            if not editor: self.unrestored.append(item)
            if editor:
                editor.restore_offset=item['offset']
                if not editor.loading: self.place(editor)
                if item['pinned']: w.notebook.set_pinned(editor,True)
                restored.append((editor,item))
        if data['layout']!='single' and len(w.notebook.pages)>1:
            side=[e for e,item in restored if item['group']==1]
            if side:
                w.notebook.set_current_page(w.notebook.page_num(side[0]))
                w.notebook.split(Gtk.Orientation.HORIZONTAL if data['layout']=='horizontal' else Gtk.Orientation.VERTICAL)
                for editor in side[1:]:
                    if w.notebook.owner(editor) is w.notebook.groups[0]:
                        w.notebook.set_current_page(w.notebook.page_num(editor)); w.notebook.move_current()
        for editor,item in restored:
            if [editor.device_id,editor.path]==data.get('active'):
                w.notebook.set_current_page(w.notebook.page_num(editor)); break
        w.sources.timeout(5000,self.tick)

    @staticmethod
    def place(editor):
        if hasattr(editor,'restore_offset'):
            offset=min(editor.restore_offset,editor.buffer.get_char_count())
            editor.buffer.place_cursor(editor.buffer.get_iter_at_offset(offset))
            editor.view.scroll_to_mark(editor.buffer.get_insert(),0.1,False,0,0)
            del editor.restore_offset

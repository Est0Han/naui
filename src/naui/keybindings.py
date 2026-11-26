"""Toolkit-independent commands and atomic, override-only shortcut profiles."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile

CONTEXTS = {'global', 'editor', 'terminal', 'file_tree', 'git', 'plugin', 'sidebar', 'search'}
MODIFIERS = ('Ctrl', 'Alt', 'Shift', 'Super')
ALIASES = {'control':'Ctrl', 'ctrl':'Ctrl', 'alt':'Alt', 'shift':'Shift', 'super':'Super', 'meta':'Super', 'cmd':'Super', 'primary':'Super' if os.sys.platform == 'darwin' else 'Ctrl'}
KEYS = {'Tab','Return','Escape','Space','BackSpace','Delete','Insert','Home','End','PageUp','PageDown','Up','Down','Left','Right','Menu'} | {f'F{i}' for i in range(1,36)}
RESERVED_TERMINAL = {'Ctrl+C', 'Ctrl+D', 'Ctrl+Z'}


def normalize(value):
    if value is None: return None
    if not isinstance(value, str) or not value or len(value) > 80: raise ValueError('Invalid shortcut')
    parts = value.strip().split('+')
    if value.endswith('++'): parts = parts[:-2] + ['+']
    modifiers = set()
    for part in parts[:-1]:
        modifier = ALIASES.get(part.strip().lower())
        if modifier is None or modifier in modifiers: raise ValueError('Invalid modifier')
        modifiers.add(modifier)
    key = parts[-1].strip()
    if len(key) == 1 and key.isprintable():
        shifted = dict(zip('~!@#$%^&*()_+{}|:"<>?', '`1234567890-=[]\\;\',./'))
        if key in shifted:
            modifiers.add('Shift'); key = shifted[key]
        upper = key.upper()
        key = upper if len(upper)==1 else key
    else:
        key = next((name for name in KEYS if name.lower() == key.lower()), None)
        if key is None: raise ValueError('Invalid shortcut key')
    return '+'.join([m for m in MODIFIERS if m in modifiers] + [key])


@dataclass
class Command:
    id: str
    title: str
    category: str
    context: str
    callback: object
    default: str = None
    enabled: object = lambda: True
    owner: str = None


class Conflict(ValueError):
    def __init__(self, commands):
        self.commands = commands
        super().__init__('Shortcut already assigned to: ' + ', '.join(c.title for c in commands))


class CommandRegistry:
    def __init__(self, path=None, on_error=None):
        self.path = Path(path) if path else None
        self.commands, self.overrides, self.listeners, self.warnings = {}, {}, [], []
        self._effective, self._groups = {}, {}
        self.on_error = on_error or (lambda error: None)
        if self.path:
            try:
                values = self.read_profile(self.path)
                for identity, value in values.items():
                    try: self.overrides[identity] = normalize(value)
                    except ValueError: self.warnings.append('Invalid binding ignored: ' + identity)
            except FileNotFoundError: pass
            except (ValueError, OSError, UnicodeError): self.warnings.append('Keybinding profile could not be loaded; defaults retained')

    @staticmethod
    def valid_id(identity):
        return isinstance(identity,str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}', identity)

    @staticmethod
    def read_profile(path):
        path = Path(path)
        if path.stat().st_size > 256 * 1024: raise ValueError('Profile too large')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or set(data) != {'version','overrides'} or type(data['version']) is not int or data['version'] != 1: raise ValueError('Unsupported keybinding profile')
        values = data['overrides']
        if not isinstance(values,dict) or len(values)>2000 or any(not CommandRegistry.valid_id(k) for k in values): raise ValueError('Invalid commands in profile')
        return values

    def register(self, command):
        if not self.valid_id(command.id) or not isinstance(command.title,str) or not command.title.strip() or len(command.title)>200 or not isinstance(command.category,str) or command.context not in CONTEXTS or not callable(command.callback) or not callable(command.enabled): raise ValueError('Invalid command')
        if command.id in self.commands: raise ValueError('Command already registered: '+command.id)
        command.default = normalize(command.default)
        self.commands[command.id] = command
        self.changed()
        return command.id

    def unregister_owner(self, owner):
        self.commands = {k:v for k,v in self.commands.items() if v.owner != owner}
        self.changed()  # Overrides survive disable/uninstall for possible reinstall.

    def requested(self, identity):
        return self.overrides.get(identity, self.commands[identity].default)

    def binding(self, identity):
        return self._effective.get(identity)  # Duplicate assignments are all unbound.

    def conflicts(self, identity, value, overlap=False):
        value = normalize(value)
        if identity not in self.commands: raise ValueError('Command is no longer available: '+str(identity))
        context = self.commands[identity].context
        return [c for c in self.commands.values() if c.id != identity and value and self.requested(c.id) == value and
                (c.context == context or overlap and 'global' in (c.context,context))]

    def set(self, identity, value, replace=False):
        value = normalize(value)
        conflicts = self.conflicts(identity,value)
        if conflicts and not replace: raise Conflict(conflicts)
        updated = dict(self.overrides)
        for command in conflicts: updated[command.id] = None
        if value == self.commands[identity].default: updated.pop(identity,None)
        else: updated[identity] = value
        self.commit(updated)

    def reset(self, identity):
        # Defaults can conflict with another customized command; caller reviews first.
        if identity not in self.commands: raise ValueError('Command is no longer available: '+str(identity))
        conflicts = self.conflicts(identity,self.commands[identity].default)
        if conflicts: raise Conflict(conflicts)
        updated = dict(self.overrides); updated.pop(identity,None)
        self.commit(updated)

    def reset_all(self): self.commit({})

    def commit(self, values):
        if self.path: self.write_profile(self.path,values)
        self.overrides = values
        self.changed()

    @staticmethod
    def write_profile(path, values):
        path = Path(path); path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.keybindings-',dir=path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream: json.dump({'version':1,'overrides':values},stream,indent=2)
            os.replace(temporary,path)
        finally: Path(temporary).unlink(missing_ok=True)

    def export(self,path): self.write_profile(path,self.overrides)

    def preview_import(self,path):
        values = {k:normalize(v) for k,v in self.read_profile(path).items()}
        candidate = dict(self.overrides,**values)
        conflicts = []
        for c in self.commands.values():
            value = candidate.get(c.id,c.default)
            if value and any(other.id != c.id and other.context == c.context and candidate.get(other.id,other.default)==value for other in self.commands.values()): conflicts.append(c.id)
        return candidate, sorted(set(conflicts)), sorted(set(values)-set(self.commands))

    def import_profile(self,path,accept_conflicts=False):
        values, conflicts, unknown = self.preview_import(path)
        if conflicts and not accept_conflicts: raise ValueError('Conflicting imported bindings: '+', '.join(conflicts))
        self.commit(values)
        return unknown

    def resolve(self, value, context):
        value = normalize(value)
        if value is None or context == 'terminal' and value in RESERVED_TERMINAL: return None
        scopes = (context, 'sidebar', 'global') if context in {'file_tree', 'search'} else (context, 'global') if context != 'global' else ('global',)
        for scope in scopes:
            identities = tuple(self._groups.get((scope,value),()))
            if len(identities) != 1: continue
            command = self.commands.get(identities[0])
            if command is not None:
                try:
                    if command.enabled() and command.id in self.commands: return command
                except Exception as error: self.on_error(error)
        return None

    def dispatch(self,value,context):
        command = self.resolve(value,context)
        if command is None: return False
        try: return command.callback() is not False
        except Exception as error:
            self.on_error(error); return True

    def close(self):
        self.listeners.clear();self.commands.clear();self._effective.clear();self._groups.clear()

    def changed(self):
        self._groups = {}
        for command in self.commands.values():
            value = self.requested(command.id)
            if value: self._groups.setdefault((command.context,value),[]).append(command.id)
        self._effective = {command.id: self.requested(command.id) if len(self._groups.get((command.context,self.requested(command.id)),()))==1 else None
                           for command in self.commands.values()}
        for callback in tuple(self.listeners):
            try: callback()
            except Exception as error: self.on_error(error)


# Stable command ID, title, default shortcut, editor controller action.
EDITOR_COMMANDS = (
 ('toggle_comment','Toggle Comment','Ctrl+/','toggle_comment'),
 ('indent','Indent Lines','Tab','indent'), ('outdent','Outdent Lines','Shift+Tab','outdent'),
 ('move_line_up','Move Lines Up','Alt+Up','move_up'), ('move_line_down','Move Lines Down','Alt+Down','move_down'),
 ('duplicate','Duplicate Lines','Ctrl+Shift+D','duplicate'), ('delete_line','Delete Lines','Ctrl+Shift+K','delete_line'),
 ('select_next','Select Next Occurrence','Ctrl+D','next_occurrence'),
 ('find','Find','Ctrl+F','find'), ('replace','Replace','Ctrl+H','replace'), ('goto_line','Go to Line','Ctrl+G','goto_line'),
 ('copy_path','Copy Path',None,'copy_path'), ('reveal','Reveal in File Tree',None,'reveal'))

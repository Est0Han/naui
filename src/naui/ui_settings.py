"""Bounded, atomic XDG presentation preferences; no geometry position/secrets."""
import json
import os
from pathlib import Path
import tempfile


DEFAULTS = dict(right_panel_width=340, right_panel_collapsed=False, right_panel_selected='', sidebar_width=300, sidebar_collapsed=False, sidebar_panel='explorer',
                sidebar_scale=100, sidebar_density='compact', transfer_height=180,
                window_width=1100, window_height=750, terminal_font=12, editor_font=12,
                editor_tab_width=4, editor_indent_width=4, editor_spaces=True,
                editor_auto_indent=True, editor_line_numbers=True, editor_current_line=True,
                editor_whitespace=False, editor_wrap=False, editor_trim=False, editor_final_newline=False,
                source_control_expanded=True, devices_expanded=True, files_expanded=True, selected_device='local',
                split_ratio=500, explorer_device_heights={}, explorer_tree_height=240, explorer_hide_disconnected=True, explorer_device_expanded={})
BOUNDS = dict(right_panel_width=(180, 800), split_ratio=(100, 900), sidebar_width=(180, 800), sidebar_scale=(80, 150), transfer_height=(80, 500),
              window_width=(640, 3840), window_height=(480, 2160),
              terminal_font=(6, 32), editor_font=(6, 32),
              editor_tab_width=(1, 16), editor_indent_width=(1, 16), explorer_tree_height=(40, 600))


class UISettings:
    def __init__(self, path):
        self.path, self.valid = Path(path), True
        self.values = dict(DEFAULTS, explorer_device_expanded={}, explorer_device_heights={})
        try:
            if self.path.stat().st_size > 16384:
                raise ValueError('UI settings too large')
            values = json.loads(self.path.read_text())
            if not isinstance(values, dict) or 'sidebar_width' not in values:
                raise ValueError('Invalid UI settings')
            self.validate(values)
            self.values.update(values)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError, UnicodeError):
            self.valid = False

    @property
    def width(self): return self.values['sidebar_width']

    @staticmethod
    def validate(values):
        for name, value in values.items():
            if name not in DEFAULTS:
                raise ValueError('Unknown UI preference')
            if name in BOUNDS:
                low, high = BOUNDS[name]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError('Invalid UI preference: ' + name)
            elif name == 'explorer_device_heights':
                if not isinstance(value, dict) or len(value) > 32: raise ValueError('Invalid tree sizes')
                for identity, height in value.items():
                    UISettings.validate({'selected_device': identity})
                    if type(height) is not int or not 40 <= height <= 1200: raise ValueError('Invalid tree height')
            elif name == 'explorer_device_expanded':
                if not isinstance(value, dict) or len(value) > 32:
                    raise ValueError('Invalid device layout')
                for identity, expanded in value.items():
                    UISettings.validate({'selected_device': identity})
                    if type(expanded) is not bool: raise ValueError('Invalid device expansion')
            elif name == 'selected_device':
                if not isinstance(value, str) or len(value) > 64 or not value or any(not (char.isascii() and (char.isalnum() or char in '-_')) for char in value):
                    raise ValueError('Invalid selected device')
            elif name == 'right_panel_selected':
                if not isinstance(value,str) or len(value)>200 or (value and (value.count(':') != 1 or any(not (c.isascii() and (c.isalnum() or c in '-_.:')) for c in value))): raise ValueError('Invalid right panel selection')
            elif name == 'sidebar_panel':
                from .keybindings import CommandRegistry
                if not CommandRegistry.valid_id(value): raise ValueError('Invalid sidebar panel')
            elif name == 'sidebar_density':
                if value not in ('compact', 'comfortable'): raise ValueError('Invalid sidebar density')
            elif type(value) is not bool:
                raise ValueError('Invalid UI preference: ' + name)

    def save(self, width=None, **values):
        if not self.valid: return
        if width is not None: values['sidebar_width'] = width
        try: self.validate(values)
        except ValueError: return
        updated = dict(self.values, **values)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.ui-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as output: json.dump(updated, output)
            os.replace(temporary, self.path)
            self.values = updated
        finally: Path(temporary).unlink(missing_ok=True)

    def reset(self):
        # Explicit Reset Layout may recover a malformed preferences file.
        self.valid = True
        self.save(**{name: DEFAULTS[name] for name in DEFAULTS
                     if not name.startswith('editor_') and name != 'terminal_font'})

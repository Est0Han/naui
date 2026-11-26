"""Pure sidebar presentation rules; never change filesystem identity."""
import posixpath
from dataclasses import dataclass

MIN_WIDTH = 220


def sidebar_width(width, window_width=0):
    """Leave useful main content space; old 180px preferences migrate safely."""
    maximum = min(800, max(MIN_WIDTH, window_width - 300)) if window_width else 800
    return max(MIN_WIDTH, min(int(width), maximum))


def breadcrumb(path):
    parts = [part for part in (path or '').split('/') if part]
    return ('… › ' if len(parts) > 3 else '/ › ') + ' › '.join(parts[-3:]) if parts else '/'


def workspace_header(device, path):
    """Presentation only: never substitute mount paths for logical paths."""
    title = root_label(path) if path else device.display_name
    backend = getattr(device.filesystem_backend, 'filesystem_label', None) if device.type == 'ssh' else None
    detail = 'Local' if device.type == 'local' else 'SSH' + (' · ' + backend if backend else '')
    return title, detail + ' · ' + device.state.title()


def transfer_badge(snapshots):
    from .transfers import FINAL_STATES
    return sum(item['state'] not in FINAL_STATES for item in snapshots), any(item['state'] == 'failed' for item in snapshots)


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    icon: str
    command: str = None


class SidebarState:
    """Native widgets stay in their stack; switching changes only an ID."""
    def __init__(self, settings):
        self.settings, self.panels, self.badges = settings, {}, {}
        self.active = None
        self.preferred = settings.values['sidebar_panel']
        self.closed = False

    def register(self, panel):
        from .keybindings import CommandRegistry
        if (self.closed or not CommandRegistry.valid_id(panel.id) or panel.id in self.panels
                or not isinstance(panel.title, str) or not panel.title.strip() or len(panel.title) > 200
                or not isinstance(panel.icon, str) or not panel.icon or len(panel.icon) > 100):
            raise ValueError('Invalid or duplicate sidebar panel')
        self.panels[panel.id] = panel
        if self.active is None or panel.id == self.preferred: self.active = panel.id

    def select(self, identity):
        if self.closed or identity not in self.panels: raise ValueError('Sidebar panel unavailable')
        self.active = self.preferred = identity
        self.settings.values['sidebar_panel'] = identity

    def remove(self, identity):
        self.panels.pop(identity, None); self.badges.pop(identity, None)
        if self.active == identity: self.active = 'explorer' if 'explorer' in self.panels else next(iter(self.panels), None)

    def badge(self, identity, count=0, failed=False):
        if identity not in self.panels: raise ValueError('Sidebar panel unavailable')
        if type(count) is not int or count < 0: raise ValueError('Invalid activity count')
        self.badges[identity] = count, bool(failed)

    def scale(self, value):
        if type(value) is not int or not 80 <= value <= 150: raise ValueError('Invalid sidebar scale')
        self.settings.values['sidebar_scale'] = value

    def close(self):
        self.closed = True


def root_label(path):
    return posixpath.basename(path.rstrip('/')) or '/'


def indentation(depth):
    # Retain hierarchy without consuming the whole row in very deep trees.
    return min(max(0, depth), 5) * 6

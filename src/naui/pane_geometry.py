"""Keep a draggable pane proportion stable across allocation and restart."""
from gi.repository import Gtk


class PaneGeometry:
    def __init__(self, pane, signals, settings):
        self.pane, self.settings = pane, settings
        self.applying, self.extent = False, 0
        signals.connect(pane, 'notify::max-position', self.restore)
        signals.connect(pane, 'notify::position', self.changed)

    def size(self):
        return self.pane.get_width() if self.pane.get_orientation() == Gtk.Orientation.HORIZONTAL else self.pane.get_height()

    def restore(self, *_):
        extent = self.size()
        if extent <= 1: return
        self.applying = True
        try:
            self.extent = extent
            self.pane.set_position(round(extent * self.settings.values['split_ratio'] / 1000))
        finally: self.applying = False

    def changed(self, *_):
        extent = self.size()
        if self.applying or extent <= 1 or extent != self.extent: return
        ratio = max(100, min(900, round(1000 * self.pane.get_position() / extent)))
        # The owner persists presentation values with the normal UI save path.
        self.settings.values['split_ratio'] = ratio

"""Archive navigation and editing of separate extracted copies."""
from pathlib import Path
from gi.repository import Gtk, Pango
from .viewers import PreviewViewer
from ...lifecycle import Signals
from ...rpf import ArchiveReader, MAX_ENTRY, child_path, hex_preview


class RpfViewer(PreviewViewer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reader = ArchiveReader(self.path)
        self.location = Path(self.path).name
        self.busy = False
        self.row_signals = Signals()
        self.toolbar = Gtk.Box(spacing=6)
        for text, callback in [('Up', self.up), ('Refresh', lambda: self.browse(self.location)),
                               ('Raw bytes', self.raw)]:
            button = Gtk.Button(label=text)
            self.signals.connect(button, 'clicked', lambda _, cb=callback: cb())
            self.toolbar.append(button)
        self.append(self.toolbar)
        note = Gtk.Label(label='Archive is read-only. Extracted copies can be edited separately; changes are not packed back into the game.', wrap=True, xalign=0)
        self.append(note)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.rows)
        self.append(scroll)

    def start(self):
        if self.remote:
            self.loading = False
            self.status.set_text('RPF browsing currently requires a local file. Copy this archive locally first.')
            self.toolbar.set_sensitive(False)
            self.update_label()
            return
        self.browse(self.location)

    def job(self, work, done):
        if self.busy or self._shutting_down: return
        self.busy = True
        self.toolbar.set_sensitive(False)
        self.rows.set_sensitive(False)
        self.status.set_text('Reading archive… large recovery scans can take up to two minutes.')
        def finished(value, error):
            if self._shutting_down or self.window._shutting_down: return
            self.busy = self.loading = False
            self.toolbar.set_sensitive(True)
            self.rows.set_sensitive(True)
            self.update_label()
            if error: self.status.set_text(str(error))
            else: done(value)
        self.window.work(work, finished)

    def browse(self, location):
        def show(rows):
            self.location = location
            self.row_signals.close()
            while self.rows.get_first_child():
                self.rows.remove(self.rows.get_first_child())
            self.status.set_text(f'{location} — {len(rows)} entries' + (' (showing first 1000)' if len(rows) > 1000 else ''))
            for entry in rows[:1000]:
                path = child_path(location, entry['path'])
                row = Gtk.Box(spacing=6)
                label = Gtk.Label(label=entry['path'], xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
                label.set_tooltip_text(path)
                row.append(label)
                if entry.get('folder'):
                    actions = [('Open', lambda p=path: self.browse(p))]
                else:
                    actions = [('Open readable', lambda p=path: self.extract(p, readable=True)),
                               ('Extract original', lambda p=path: self.extract(p))]
                for title, callback in actions:
                    button = Gtk.Button(label=title)
                    button.set_sensitive(entry.get('folder') or (entry.get('size') or 0) <= MAX_ENTRY)
                    self.row_signals.connect(button, 'clicked', lambda _, cb=callback: cb())
                    row.append(button)
                self.rows.append(row)
        reader = self.reader
        self.job(lambda: reader.listing(location), show)

    def up(self):
        if '/' in self.location: self.browse(self.location.rsplit('/', 1)[0])

    def extract(self, path, xml=False, readable=False):
        def opened(destination):
            self.status.set_text('Extracted copy (not saved back to archive): ' + destination)
            self.window.notebook.keep_open(self)
            self.window.open_path(self.window.local_backend, destination, preview=False)
        reader = self.reader
        self.job(lambda: reader.readable(path) if readable else reader.extract(path, xml=xml), opened)

    def raw(self):
        def shown(content):
            self.window.notebook.keep_open(self)
            self.window.open_text_view(self.device_id, 'Raw bytes: ' + Path(self.path).name,
                                       'First 4096 archive bytes (hexadecimal; not source code)\n\n' + content, None)
        path = self.path
        self.job(lambda: hex_preview(path), shown)

    def stop(self):
        self.row_signals.close()
        self.reader.close()

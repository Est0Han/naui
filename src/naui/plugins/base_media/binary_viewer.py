"""Generic binary preview using the shared local/SSH cache."""
from gi.repository import Gtk
from .viewers import PreviewViewer
from ...binary import inspect_binary


class BinaryViewer(PreviewViewer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pages = Gtk.Stack(hexpand=True, vexpand=True)
        self.append(Gtk.StackSwitcher(stack=self.pages))
        self.append(self.pages)

    def start(self):
        cache, backend, path, remote = self.cache, self.backend, self.path, self.remote
        def inspect():
            source = cache.prepare(backend, path, remote)
            try:
                return source, inspect_binary(source.local_path)
            except Exception:
                cache.release(source)
                raise
        def ready(result, error):
            if self._shutting_down or self.window._shutting_down:
                if result: cache.release(result[0])
                return
            self.loading = False
            if error:
                self.status.set_text('Cannot inspect binary: ' + str(error))
            else:
                self.source, reports = result
                for title, content in reports.items():
                    view = Gtk.TextView(editable=False, cursor_visible=True, monospace=True,
                                        wrap_mode=Gtk.WrapMode.WORD_CHAR)
                    view.set_left_margin(8); view.set_right_margin(8)
                    view.get_buffer().set_text(content)
                    scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
                    scroll.set_child(view)
                    self.pages.add_titled(scroll, title, title)
                self.status.set_text('Read-only binary preview • limited to the first 8 MiB')
            self.update_label()
        self.window.work(inspect, ready)

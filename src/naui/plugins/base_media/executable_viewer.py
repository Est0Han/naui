"""Read-only executable inspection with the existing local/SSH preview transport."""
import threading
from gi.repository import Gtk, GtkSource
from ...decompiler import decompile
from .viewers import PreviewViewer
from ...executable import inspect_executable


class ExecutableViewer(PreviewViewer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_decompile = threading.Event()
        self.decompile_running = False
        self.code_button = Gtk.Button(label='Decompile to C-like code')
        self.code_button.set_sensitive(False)
        self.signals.connect(self.code_button, 'clicked', lambda *_: self.show_code())
        self.append(self.code_button)
        self.pages = Gtk.Stack(hexpand=True, vexpand=True)
        switcher = Gtk.StackSwitcher(stack=self.pages)
        self.append(switcher)
        self.append(self.pages)

    def start(self):
        cache, backend, path, remote = self.cache, self.backend, self.path, self.remote
        def inspect():
            source = cache.prepare(backend, path, remote)
            try:
                return source, inspect_executable(source.local_path)
            except Exception:
                cache.release(source)
                raise
        def ready(result, error):
            if self._shutting_down or self.window._shutting_down:
                if result: cache.release(result[0])
                return
            self.loading = False
            if error:
                self.status.set_text('Cannot inspect executable: ' + str(error))
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
                self.status.set_text('Read-only • choose Decompile for reconstructed C-like code')
                self.code_button.set_sensitive(True)
            self.update_label()
        self.window.work(inspect, ready)

    def show_code(self):
        if self.decompile_running:
            self.cancel_decompile.set()
            self.code_button.set_sensitive(False)
            return
        if not self.source: return
        self.cancel_decompile = threading.Event()
        self.decompile_running = True
        self.code_button.set_label('Cancel decompilation')
        self.status.set_text('Decompiling… this can take up to four minutes. The file is not executed.')
        path, cancelled = self.source.local_path, self.cancel_decompile
        def finished(content, error):
            if self._shutting_down or self.window._shutting_down: return
            self.decompile_running = False
            self.code_button.set_sensitive(True)
            self.code_button.set_label('Decompile to C-like code')
            if error:
                self.status.set_text(str(error)); return
            old = self.pages.get_child_by_name('Code')
            if old: self.pages.remove(old)
            buffer = GtkSource.Buffer()
            buffer.set_language(GtkSource.LanguageManager.get_default().get_language('c'))
            scheme = GtkSource.StyleSchemeManager.get_default().get_scheme('Adwaita-dark')
            if scheme: buffer.set_style_scheme(scheme)
            buffer.set_text(content)
            view = GtkSource.View(buffer=buffer, editable=False, monospace=True,
                                  show_line_numbers=True, wrap_mode=Gtk.WrapMode.NONE)
            scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            scroll.set_child(view)
            self.pages.add_titled(scroll, 'Code', 'Code')
            self.pages.set_visible_child_name('Code')
            self.status.set_text('Reconstructed C-like code • partial preview • not original source')
        self.window.work(lambda: decompile(path, cancelled), finished)

    def close(self):
        # Stop analysis when its owning tab is closed.
        self.cancel_decompile.set()
        super().close()

"""GTK smoke: RPF routing, navigation, XML copy and normal editor ownership."""
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
import gi
for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('GtkSource', '5'), ('Vte', '3.91')]:
    gi.require_version(name, version)
from gi.repository import GLib
from naui.gui import NauiApplication


def main():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / 'sample.rpf'
        archive.write_bytes(b'9FPR' + b'\0' * 32)
        os.environ.update(XDG_CONFIG_HOME=directory+'/config', XDG_CACHE_HOME=directory+'/cache', XDG_DATA_HOME=directory+'/data')
        app = NauiApplication([], directory)
        state = {'step': 0}; errors = []; deadline = time.monotonic() + 25
        def run(reader, *args):
            if args[0] == '--json':
                return b'[{"path":"metadata.psin","folder":false,"size":100}]'
            if args[0] == 'xml': return b'<root>original</root>'
            raise AssertionError(args)
        def tick():
            w = app.window
            try:
                assert time.monotonic() < deadline, state
                if state['step'] == 0 and w.at_prompt:
                    w.open_path(w.local_backend, str(archive)); state['step'] = 1
                elif state['step'] == 1 and w.viewers and not w.viewers[-1].loading:
                    viewer = w.viewers[-1]; state['viewer'] = viewer
                    assert viewer.rows.get_first_child() is not None
                    viewer.rows.get_first_child().get_child().get_first_child().get_next_sibling().emit('clicked'); state['step'] = 2
                elif state['step'] == 2 and w.editors and not w.editors[-1].loading:
                    editor = w.editors[-1]
                    assert editor.path != str(archive)
                    assert Path(editor.path).read_text() == '<root>original</root>'
                    editor.buffer.insert(editor.buffer.get_end_iter(), '\n<!-- edit -->')
                    assert editor.dirty
                    assert state['viewer'] in w.viewers
                    assert archive.read_bytes() == b'9FPR' + b'\0' * 32
                    w.close_viewer(state['viewer'])
                    assert state['viewer'].reader.cancelled.is_set()
                    state['step'] = 3
                    w.shutdown(); w.destroy(); app.quit(); return False
            except Exception as error:
                import traceback; traceback.print_exc()
                errors.append(str(error)); w.shutdown(); w.destroy(); app.quit(); return False
            return True
        with patch('naui.rpf.ArchiveReader.run', run):
            app.connect_after('activate', lambda *_: GLib.timeout_add(100, tick))
            app.run(['rpf-smoke'])
        assert not errors, errors
        assert state['step'] == 3, state
        print('PASS: archive tab, listing, extracted XML editor, dirty copy, original preserved, close cancellation')

if __name__ == '__main__': main()

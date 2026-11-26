#!/usr/bin/env python3
"""Desktop-only core transfers, rendered conflicts and local exit N/EOF/Y."""
import os
from pathlib import Path
import sys
import tempfile
import time

import gi
for name, version in [('Gtk','4.0'), ('Gdk','4.0'), ('Vte','3.91'), ('GtkSource','5')]:
    gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources
from naui.transfers import FileLocation


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; rendered transfer/terminal exit not tested.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-transfer-smoke-') as directory:
        root = Path(directory)
        for name in ('runtime', 'config', 'cache', 'dest'):
            (root / name).mkdir(mode=0o700)
        os.environ.update(XDG_RUNTIME_DIR=str(root/'runtime'), XDG_CONFIG_HOME=str(root/'config'), XDG_CACHE_HOME=str(root/'cache'))
        source = root / 'test õ.txt'; source.write_text('Hello transfer\n')
        (root / 'dest/test õ.txt').write_text('existing destination\n')
        app = NauiApplication([], directory)
        sources, retained = Sources(), []
        state = {'stage': 'ready', 'error': None}
        deadline = time.monotonic() + 45
        def tick():
            window = app.window
            try:
                assert time.monotonic() < deadline, state['stage']
                if state['stage'] == 'ready' and window.at_prompt:
                    state['first'] = window.terminal
                    state['job'] = window.transfers.copy(FileLocation('local', str(source)), FileLocation('local', str(root/'dest/test õ.txt')))
                    state['stage'] = 'conflict'
                elif state['stage'] == 'conflict' and window.transfer_panel.conflict_dialog:
                    assert (root/'dest/test õ.txt').read_text() == 'existing destination\n'
                    window.dialog.response(1)  # Keep Both in the native dialog.
                    state['stage'] = 'copied'
                elif state['stage'] == 'copied' and window.transfers.status(state['job'])['state'] == 'completed':
                    assert (root/'dest/test õ (1).txt').read_text() == source.read_text()
                    assert source.exists()
                    assert not list((root/'dest').glob('.naui-partial-*'))
                    window.terminal.send_input(b'exit\n')
                    state['stage'] = 'exit-n'
                elif state['stage'] == 'exit-n' and window.dialog and window.dialog.get_title() == 'Exit Naui?':
                    assert state['first'].returncode == 0
                    assert not window._shutting_down
                    window.dialog.response(Gtk.ResponseType.CANCEL)  # N/Escape behavior.
                    state['stage'] = 'restarted'
                elif state['stage'] == 'restarted' and window.at_prompt and window.terminal is not state['first']:
                    state['second'] = window.terminal
                    assert len(window.terminals) == 1
                    window.terminal.send_input(('nano ' + str(root/'opened.txt') + '\n').encode())
                    state['stage'] = 'nano'
                elif state['stage'] == 'nano' and window.editors and not window.editors[0].loading and window.at_prompt:
                    assert window.editors[0].device_id == 'local'
                    editor = window.editors[0]
                    editor.buffer.set_text('Hello from Naui\n'); editor.save()
                    state['stage'] = 'saved'
                elif state['stage'] == 'saved' and not window.editors[0].saving:
                    assert (root/'opened.txt').read_text() == 'Hello from Naui\n'
                    state['second'].send_input(b'\x04')  # Let Bash handle real EOF.
                    state['stage'] = 'exit-y'
                elif state['stage'] == 'exit-y' and window.dialog and window.dialog.get_title() == 'Exit Naui?':
                    assert state['second'].returncode == 0
                    sources.close()
                    window.dialog.response(Gtk.ResponseType.ACCEPT)  # Y: normal close path.
                    state['stage'] = 'closed'
                    return
                sources.timeout(30, tick)
            except Exception as error:
                state['error'] = error
                sources.close()
                window._discard_approved = window._transfers_exit_approved = True
                window.close()
        def activated(application):
            retained.append(application.window)
            sources.timeout(30, tick)
        handler = app.connect_after('activate', activated)
        try:
            result = app.run([sys.argv[0]])
        finally:
            sources.close(); app.disconnect(handler)
        if state['error']:
            raise state['error']
        assert result == 0 and app.window is None and state['stage'] == 'closed'
        window = retained[0]
        assert window._shutting_down and window.plugins.closed and window.authentication.closed
        assert window.transfers.closed and window.transfers.resolve_device is None
        assert window.transfer_panel.window is None and window.code_navigation.window is None
        assert not window.sources._ids
        assert all(terminal.bridge.process.returncode is not None for terminal in (state['first'], state['second']))
        print('PASS: rendered Keep Both/copy, nano/editor/Ctrl+S, local exit restart, EOF exit and clean shutdown')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Real GTK/VTE split-view acceptance with disposable files and configuration."""
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import gi
for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('GtkSource', '5'), ('Vte', '3.91')]:
    gi.require_version(name, version)
from gi.repository import Gtk, Gdk, GLib
from naui.gui import NauiApplication


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-split-smoke-') as directory:
        root = Path(directory)
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'), XDG_CACHE_HOME=str(root/'cache'), SHELL='/bin/bash')
        a, b = root/'first.txt', root/'second.txt'
        a.write_text('first\n'); b.write_text('second\n')
        app = NauiApplication([], directory)
        state, errors = {'stage': 0}, []
        deadline = time.monotonic() + 30
        def tick():
            w = app.window
            if w is None: return True
            tabs = w.notebook
            try:
                assert time.monotonic() < deadline, state
                if state['stage'] == 0 and w.at_prompt:
                    state['terminal'] = w.terminal
                    state['process'] = w.terminal.bridge.process
                    w.open_file(w.local_backend, str(a), preview=False)
                    w.open_file(w.local_backend, str(b), preview=False)
                    state['stage'] = 1
                elif state['stage'] == 1 and len(w.editors) == 2 and all(not e.loading for e in w.editors):
                    first, second = w.editors
                    state.update(first=first, second=second)
                    first.buffer.insert(first.buffer.get_end_iter(), 'changed in split\n')
                    assert first.dirty
                    tabs.set_current_page(tabs.page_num(second))
                    assert tabs.split(Gtk.Orientation.HORIZONTAL)
                    state['stage'] = 2
                elif state['stage'] == 2:
                    first, second = state['first'], state['second']
                    assert len(tabs.groups) == 2
                    assert tabs.owner(first) is not tabs.owner(second)
                    assert all(group.get_width() > 0 for group in tabs.groups)
                    assert all(group.get_width() > tabs.get_width() * 0.3 for group in tabs.groups), 'A pane was squeezed by its toolbar'
                    assert second.get_mapped()
                    tabs.set_current_page(tabs.page_num(first))
                    assert first.get_mapped()
                    assert w.current_editor() is first and first.dirty
                    assert first.buffer.get_can_undo()
                    assert w.key_pressed(None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK)
                    state['stage'] = 3
                elif state['stage'] == 3 and not state['first'].saving:
                    first, second = state['first'], state['second']
                    assert a.read_text() == 'first\nchanged in split\n'
                    assert b.read_text() == 'second\n'
                    second.view.grab_focus()
                    state['stage'] = 4
                elif state['stage'] == 4:
                    first, second = state['first'], state['second']
                    assert w.current_editor() is second, 'Focus did not select the other group'
                    assert tabs.split(Gtk.Orientation.VERTICAL)
                    assert tabs.paned.get_orientation() == Gtk.Orientation.VERTICAL
                    assert tabs.current is second
                    tabs.set_current_page(tabs.page_num(first))
                    assert tabs.move_current()
                    assert tabs.owner(first) is tabs.owner(second)
                    w.close_editor(first)
                    state['stage'] = 5
                elif state['stage'] == 5 and state['first'] not in w.editors:
                    second = state['second']
                    tabs.set_current_page(tabs.page_num(second))
                    assert tabs.move_current()
                    assert len(tabs.groups) == 1, 'Empty group did not collapse'
                    terminal = state['terminal']
                    tabs.set_current_page(tabs.page_num(terminal))
                    assert tabs.split(Gtk.Orientation.HORIZONTAL)
                    assert not w.key_pressed(None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK)
                    assert terminal.bridge.process is state['process']
                    assert state['process'].poll() is None
                    marker = root/'terminal-survived'
                    state['marker'] = marker
                    terminal.send_input(('printf ok > ' + shlex.quote(str(marker)) + '\n').encode())
                    state['stage'] = 6
                elif state['stage'] == 6 and state['marker'].exists():
                    assert state['marker'].read_text() == 'ok'
                    tabs.single_view()
                    assert len(tabs.groups) == 1 and tabs.get_n_pages() == 2
                    # Repeated moves must preserve widgets and release discarded groups.
                    for _ in range(12):
                        assert tabs.split(Gtk.Orientation.VERTICAL)
                        tabs.single_view()
                    assert tabs.get_nth_page(tabs.page_num(state['second'])) is state['second']
                    tabs.set_current_page(tabs.page_num(state['second']))
                    assert tabs.split(Gtk.Orientation.HORIZONTAL)
                    w.close_editor(state['second'])
                    assert len(tabs.groups) == 1 and tabs.get_n_pages() == 1
                    assert tabs.current is state['terminal']
                    state['stage'] = 7
                    w.close()
                    return False
            except Exception as error:
                errors.append(error)
                w._discard_approved = True
                w.close()
                return False
            return True
        source = GLib.timeout_add(150, tick)
        app.run([sys.argv[0]])
        if GLib.MainContext.default().find_source_by_id(source): GLib.source_remove(source)
        if errors: raise errors[0]
        assert state['stage'] == 7
    print('PASS: both split layouts, focus and save routing, dirty/undo preservation, move/close/merge, live PTY survival, repeated split teardown')
    return 0


if __name__ == '__main__': raise SystemExit(main())

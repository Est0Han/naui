#!/usr/bin/env python3
"""Native two/three-tab reproduction plus 50 closes with one survivor.

Run from a desktop: PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings
python3 tests/terminal_tabs_smoke.py
Optional --ssh-host runs two dedicated OpenSSH terminals, closes one and keeps
its sibling/device available. Only use a target you authorize connecting to.
"""
import argparse
import gc
import os
import shlex
from pathlib import Path
import sys
import tempfile
import time
import gi

for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('Vte', '3.91'), ('GtkSource', '5')]:
    gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host')
    args = parser.parse_args()
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; native VTE tab-close acceptance was not run.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-terminal-smoke-') as directory:
        root = Path(directory)
        for name in ('config', 'cache'): (root/name).mkdir(mode=0o700)
        # Keep the desktop's XDG_RUNTIME_DIR: GTK already opened its Wayland
        # connection and native components may still resolve resources there.
        # Naui owns unique temporary runtime directories and cleans them itself.
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'),
                          XDG_CACHE_HOME=str(root/'cache'), SHELL='/bin/bash')
        print('START: native terminal close test; desktop runtime directory preserved', flush=True)
        app = NauiApplication([], str(root))
        sources, retained, errors = Sources(), [], []
        state = dict(stage='start', closes=0, target=None)
        deadline = time.monotonic()+180
        old_hook = sys.excepthook
        def exception(kind, value, traceback):
            errors.append(value)
            old_hook(kind, value, traceback)
        sys.excepthook = exception

        def fail(error):
            if not errors: errors.append(error)
            sources.close()
            app.window._discard_approved = True
            app.window.close()

        def tick():
            window = app.window
            try:
                if errors: raise errors[0]
                assert time.monotonic() < deadline, state
                local = window.workspace.devices['local']
                assert not window._shutting_down, 'Closing a terminal shut down Naui'
                if state['stage'] == 'start':
                    retained.append(window)
                    while len(window.terminals) < 2: window.create_terminal(local, str(root))
                    state['stage'] = 'close'
                elif state['stage'] == 'close' and all(runtime.at_prompt for runtime in window.runtimes.values() if not runtime.closed):
                    identities = list(window.terminals)
                    # Exactly two tabs: close second, then first. Next close
                    # the middle of three and the first survivor. Finally keep
                    # one fixed shell alive through 50 new-tab closes.
                    index = (1, 0, 1, 0)[state['closes']] if state['closes'] < 4 else 1
                    identity = identities[index]
                    state['survivors'] = {key: window.terminals[key] for key in identities if key != identity}
                    state['target'] = identity
                    window.terminals[identity].tab_close_button.emit('clicked')
                    assert identity in window.terminals  # Widget still owned during clicked.
                    assert window.runtimes[identity].closing
                    state['stage'] = 'removed'
                elif state['stage'] == 'removed' and state['target'] not in window.terminals:
                    state['markers'] = {}
                    for identity, terminal in state['survivors'].items():
                        assert window.terminals[identity] is terminal
                        assert terminal.bridge.process.poll() is None
                        assert not window.runtimes[identity].closed
                        assert not window.runtimes[identity].closing
                        marker = root/('survivor-' + str(state['closes']) + '-' + identity)
                        text = 'alive-' + str(state['closes']) + '-' + identity
                        state['markers'][marker] = text
                        terminal.send_input(('echo hello; printf %s ' + shlex.quote(text)
                                             + ' > ' + shlex.quote(str(marker)) + '\n').encode())
                    # Collect wrappers after every actual native page removal.
                    gc.collect()
                    state['stage'] = 'survivors'
                elif state['stage'] == 'survivors' and all(path.exists() and path.read_text() == text
                                                        for path, text in state['markers'].items()):
                    for path, text in state['markers'].items():
                        assert path.read_text() == text, 'Surviving terminal stopped executing commands'
                    state['closes'] += 1
                    if state['closes'] <= 4 or (state['closes'] - 4) % 10 == 0:
                        print('Verified terminal closes:', state['closes'],
                              '; all survivors executed commands after GC', flush=True)
                    if state['closes'] < 54:
                        count = 3 if state['closes'] == 2 else 2
                        while len(window.terminals) < count: window.create_terminal(local, str(root))
                        state['stage'] = 'close'
                    else:
                        assert len(window.terminals) == 1
                        if args.ssh_host:
                            device = window.add_ssh_device(args.ssh_host)
                            state['device'] = device; state['stage'] = 'ssh-first'
                        else:
                            state['stage'] = 'done'; sources.close(); window.close()
                elif state['stage'] == 'ssh-first' and state['device'].connected and window.at_prompt:
                    state['first'] = window.runtime.id
                    window.connect_device(state['device'], new_terminal=True)
                    state['second'] = window.runtime.id; state['stage'] = 'ssh-two'
                elif state['stage'] == 'ssh-two' and window.at_prompt:
                    assert window.runtimes[state['first']].at_prompt
                    window.terminals[state['first']].tab_close_button.emit('clicked')
                    state['stage'] = 'ssh-one'
                elif state['stage'] == 'ssh-one' and state['first'] not in window.terminals:
                    assert state['device'].connected
                    assert state['second'] in window.terminals
                    assert window.terminals[state['second']].bridge.process.poll() is None
                    window.disconnect_device(state['device'].id)
                    assert state['device'].state == 'disconnected'
                    state['stage'] = 'done'; sources.close(); window.close()
            except Exception as error:
                fail(error); return
            if state['stage'] != 'done': sources.timeout(30, tick)

        handler = app.connect_after('activate', lambda *_: sources.timeout(30, tick))
        try: result = app.run([sys.argv[0]])
        finally:
            sources.close(); app.disconnect(handler); sys.excepthook = old_hook
        if errors: raise errors[0]
        assert state['stage'] == 'done' and result == 0, (state, result)
        window = retained.pop()
        assert window._shutting_down
        assert all(terminal.bridge.process.poll() is not None for terminal in window.terminals.values())
        del window, app
        gc.collect()
        print('PASS: both two-tab close orders, three-tab middle/first closes, 50 stress closes; '
              'every survivor executes commands after each close/GC; normal exit 0')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

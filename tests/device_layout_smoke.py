#!/usr/bin/env python3
"""Real GTK device-card geometry/persistence check, using disconnected fixtures only."""
import os
from pathlib import Path
import sys
import tempfile
import time

import gi
for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('Vte', '3.91'), ('GtkSource', '5')]:
    gi.require_version(name, version)
from gi.repository import Gdk, GLib, Gtk
from naui.gui import NauiApplication
from naui.ui_settings import UISettings


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no graphical display')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-device-layout-') as directory:
        root = Path(directory)
        os.environ.update(XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'))
        for n in range(30): (root / f'file-{n}.txt').write_text('fixture\n')
        app = NauiApplication([], directory)
        state, errors = {'stage': 0}, []
        deadline = time.monotonic() + 25

        def tick():
            window = app.window
            if window is None: return True
            sidebar = window.workspace_sidebar
            try:
                assert time.monotonic() < deadline, state
                if state['stage'] == 0 and window.at_prompt:
                    remotes = [window.workspace.add_ssh(f'fixture-{n}.invalid', f'Server {n}') for n in range(5)]
                    for device in remotes:
                        device.navigation_state.current_path = '/home/user'
                        sidebar.add_device(device)
                        assert not sidebar.cards[device.id][0].get_visible()
                    assert sidebar.hide_disconnected.get_active()
                    sidebar.hide_disconnected.set_active(False)
                    state['remotes'] = remotes
                    state['tree'] = sidebar.explorers['local'].tree_model
                    state['stage'] = 1
                elif state['stage'] == 1:
                    previous_bottom = None
                    for device in state['remotes']:
                        card = sidebar.cards[device.id][0]
                        assert not card.get_expanded()
                        assert 0 < card.get_height() < 50, card.get_height()
                        allocation = card.get_allocation()
                        if previous_bottom is not None:
                            assert 0 <= allocation.y - previous_bottom <= 3
                        previous_bottom = allocation.y + allocation.height
                    assert abs(previous_bottom - sidebar.container.get_height()) <= 3
                    card = sidebar.cards[state['remotes'][0].id][0]
                    card.set_expanded(True)
                    sidebar.tree_height.set_value(80)
                    state['stage'] = 2
                elif state['stage'] == 2:
                    local = sidebar.explorers['local']
                    assert 0 < local.scroll.get_height() <= 82, local.scroll.get_height()
                    card = sidebar.cards[state['remotes'][0].id][0]
                    assert card.get_height() < 200, card.get_height()
                    sidebar.hide_disconnected.set_active(True)
                    for device in state['remotes']:
                        assert not sidebar.cards[device.id][0].get_visible()
                        assert sidebar.device_rows[device.id].get_visible()
                        assert device.state == 'disconnected'
                    assert sidebar.cards['local'][0].get_visible()
                    assert local.tree_model is state['tree']
                    sidebar.cards['local'][0].set_expanded(False)
                    restored = UISettings(window.ui_settings.path)
                    assert restored.valid
                    assert restored.values['explorer_tree_height'] == 80
                    assert restored.values['explorer_hide_disconnected']
                    assert not restored.values['explorer_device_expanded']['local']
                    assert restored.values['explorer_device_expanded'][state['remotes'][0].id]
                    sidebar.update_devices()
                    assert not sidebar.cards['local'][0].get_expanded()
                    sidebar.hide_disconnected.set_active(False)
                    assert sidebar.cards[state['remotes'][0].id][0].get_expanded()
                    state['stage'] = 3
                elif state['stage'] == 3:
                    assert 0 < sidebar.cards['local'][0].get_height() < 50
                    window.reset_layout()
                    assert sidebar.cards['local'][0].get_expanded()
                    assert not sidebar.cards[state['remotes'][0].id][0].get_expanded()
                    assert sidebar.tree_height.get_value_as_int() == 240
                    assert sidebar.hide_disconnected.get_active()
                    state['stage'] = 4
                    window.close()
                    return False
            except Exception as error:
                errors.append(error)
                window._discard_approved = True
                window.close()
                return False
            return True

        source = GLib.timeout_add(200, tick)
        app.run([sys.argv[0]])
        if GLib.MainContext.default().find_source_by_id(source): GLib.source_remove(source)
        if errors: raise errors[0]
        assert state['stage'] == 4
    print('PASS: compact collapsed cards, content-sized trees, adjustable height, hide/restore, retained trees, saved preferences and reset')
    return 0


if __name__ == '__main__': raise SystemExit(main())

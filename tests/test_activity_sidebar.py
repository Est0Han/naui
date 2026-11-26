"""Activity navigation models and controller contracts, without pixel assertions."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk

from naui.sidebar_layout import Panel, SidebarState, sidebar_width, breadcrumb, workspace_header, transfer_badge
from naui.ui_settings import UISettings
from naui.sidebar import SidebarManager, ACTIVITIES, settings_panel
from naui.keybindings import Command, CommandRegistry
from naui.keybinding_host import focused_context, register_core
from naui.gui import NauiWindow
from naui.workspace import RemoteDeviceBackend
from naui.plugin_api import DeviceInfo, PluginAPI, UIAPI
from naui.plugins.base_git import GitPlugin, Repository
from naui.plugins.git_service import parse_status


class SidebarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = UISettings(Path(self.temp.name) / 'ui.json')
        self.state = SidebarState(self.settings)
        for panel in ACTIVITIES: self.state.register(panel)

    def test_one_activity_and_registration_validation(self):
        self.assertEqual(self.state.active, 'explorer')
        self.assertEqual(set(self.state.panels), {'explorer', 'search', 'source_control', 'transfers', 'devices', 'settings'})
        with self.assertRaises(ValueError): self.state.register(ACTIVITIES[0])
        with self.assertRaises(ValueError): self.state.register(Panel('../bad', 'Bad', 'folder-symbolic'))
        with self.assertRaises(ValueError): self.state.select('unknown')
        for panel in (Panel('bad-title', None, 'folder-symbolic'), Panel('bad-title', '', 'folder-symbolic'), Panel('bad-icon', 'Title', None)):
            with self.assertRaises(ValueError): self.state.register(panel)
        self.assertNotIn('bad-title', self.state.panels)
        self.assertNotIn('bad-icon', self.state.panels)
        self.assertEqual(self.state.active, 'explorer')

    def test_panel_selection_persistence_and_unavailable_restore(self):
        self.state.select('source_control'); self.settings.save()
        restored = SidebarState(UISettings(self.settings.path))
        for panel in ACTIVITIES: restored.register(panel)
        self.assertEqual(restored.active, 'source_control')
        restored.select('devices'); restored.select('explorer')
        self.settings.save(sidebar_panel='plugin.example.panel')
        restored = SidebarState(UISettings(self.settings.path))
        for panel in ACTIVITIES: restored.register(panel)
        self.assertEqual(restored.active, 'explorer')
        restored.register(Panel('plugin.example.panel', 'Example', 'folder-symbolic'))
        self.assertEqual(restored.active, 'plugin.example.panel')
        restored.remove('plugin.example.panel')
        self.assertEqual(restored.active, 'explorer')
        self.assertEqual(restored.settings.values['sidebar_panel'], 'plugin.example.panel')

    def test_user_selection_wins_over_late_plugin_restore(self):
        self.settings.values['sidebar_panel'] = 'plugin.example.panel'
        state = SidebarState(self.settings)
        state.register(ACTIVITIES[0]); state.select('explorer')
        state.register(Panel('plugin.example.panel', 'Example', 'folder-symbolic'))
        self.assertEqual(state.active, 'explorer')

    def test_width_scale_density_reset_and_font_isolation(self):
        self.settings.save(700, sidebar_scale=150, sidebar_density='comfortable', editor_font=18, terminal_font=16)
        restored = UISettings(self.settings.path)
        self.assertEqual((restored.width, restored.values['sidebar_scale'], restored.values['sidebar_density']), (700, 150, 'comfortable'))
        self.assertEqual([sidebar_width(value, 1100) for value in (180, 220, 300, 500, 700)], [220, 220, 300, 500, 700])
        self.assertEqual(sidebar_width(700, 640), 340)
        restored.reset()
        self.assertEqual((restored.width, restored.values['sidebar_scale'], restored.values['sidebar_density']), (300, 100, 'compact'))
        self.assertEqual((restored.values['editor_font'], restored.values['terminal_font']), (18, 16))

    def test_invalid_presentation_settings_are_nonfatal(self):
        for value in (79, 151, '125', True):
            with self.assertRaises(ValueError): self.state.scale(value)
        for changes in ({'sidebar_panel': '../escape'}, {'sidebar_density': 'unknown'}, {'sidebar_density': []}, {'sidebar_scale': 151}):
            with self.assertRaises(ValueError): UISettings.validate(changes)
        self.state.close(); self.state.close()
        with self.assertRaises(ValueError): self.state.select('explorer')

    def test_device_layout_preferences_round_trip_and_reset(self):
        self.settings.save(explorer_tree_height=80, explorer_hide_disconnected=True,
                           explorer_device_expanded={'local': False, 'ssh-example': True})
        restored = UISettings(self.settings.path)
        self.assertTrue(restored.valid)
        self.assertEqual(restored.values['explorer_tree_height'], 80)
        self.assertTrue(restored.values['explorer_hide_disconnected'])
        self.assertEqual(restored.values['explorer_device_expanded'], {'local': False, 'ssh-example': True})
        restored.reset()
        self.assertEqual(restored.values['explorer_device_expanded'], {})
        self.assertTrue(restored.values['explorer_hide_disconnected'])
        for changes in ({'explorer_tree_height': 39}, {'explorer_tree_height': 601},
                        {'explorer_device_expanded': {'local': 1}},
                        {'explorer_device_expanded': {'../bad': True}},
                        {'explorer_device_expanded': {str(n): True for n in range(33)}}):
            with self.assertRaises(ValueError): UISettings.validate(changes)

    def test_badges_and_retained_inactive_attention(self):
        self.state.badge('source_control', 11)
        self.state.badge('transfers', 2, True)
        for panel in ('source_control', 'transfers', 'devices', 'explorer'): self.state.select(panel)
        self.assertEqual(self.state.badges, {'source_control': (11, False), 'transfers': (2, True)})
        with self.assertRaises(ValueError): self.state.badge('source_control', -1)
        with self.assertRaises(ValueError): self.state.badge('source_control', True)

    def test_logical_headers_and_breadcrumbs(self):
        local = SimpleNamespace(display_name='workstation', type='local', state='connected', filesystem_backend=object())
        remote = SimpleNamespace(display_name='example-server', type='ssh', state='connected', filesystem_backend=SimpleNamespace(filesystem_label='SSHFS'))
        self.assertEqual(workspace_header(local, '/home/user/code/project'), ('project', 'Local · Connected'))
        self.assertEqual(workspace_header(remote, '/opt/project'), ('project', 'SSH · SSHFS · Connected'))
        remote.state = 'disconnected'; remote.filesystem_backend.filesystem_label = 'Compatibility'
        self.assertEqual(workspace_header(remote, None), ('example-server', 'SSH · Compatibility · Disconnected'))
        self.assertEqual(breadcrumb('/home/user/code/project/src'), '… › code › project › src')
        self.assertEqual(breadcrumb('/opt/project'), '/ › opt › project')
        self.assertEqual(breadcrumb('/'), '/')
        # Long components are passed to a native ellipsized label, never truncated identity.
        path = '/opt/' + 'long-project-' * 30
        self.assertTrue(breadcrumb(path).endswith('long-project-' * 30))
        self.assertEqual(workspace_header(remote, path)[0], 'long-project-' * 30)

    def test_filesystem_header_never_probes_or_mounts(self):
        backend = RemoteDeviceBackend('example-server')
        with patch('naui.sshfs.detect_support', side_effect=AssertionError('UI probed external tools')):
            self.assertEqual(backend.filesystem_label, 'Compatibility')
            backend._sshfs = SimpleNamespace(mounted=True, warning='')
            self.assertEqual(backend.filesystem_label, 'SSHFS')
            backend._sshfs.warning = 'disconnected'
            self.assertEqual(backend.filesystem_label, 'Compatibility')

    def test_transfer_count_not_completed_rows(self):
        snapshots = [{'state': state} for state in ('queued', 'running', 'completed', 'failed', 'cancelled')]
        self.assertEqual(transfer_badge(snapshots), (2, True))
        self.assertEqual(transfer_badge([]), (0, False))

    def test_git_badge_counts_unique_files_not_staged_plus_worktree(self):
        badges = []
        device = DeviceInfo('local', 'local', 'Workstation', 'connected', '/project', ('/project',), active=True)
        api = SimpleNamespace(devices=lambda: [device], on_workspace_changed=lambda *_: None,
            add_command=lambda *_, **kw: None, sidebar=lambda *_: None,
            selected_location=lambda: SimpleNamespace(device_id='local', path='/project'),
            ui=SimpleNamespace(set_sidebar_badge=lambda *args: badges.append(args)))
        plugin = GitPlugin(api)
        status = parse_status(b'# branch.head main\x00' + b'1 MM N... 100644 100644 100644 a b main.py\0? new.py\0')
        plugin.repositories[('local', '/project')] = Repository('local', '/project', status=status)
        plugin.render()
        self.assertEqual(badges[-1], ('status', 2))

    def test_switching_keeps_widgets_and_their_panel_state(self):
        retained = {p.id: object() for p in ACTIVITIES}
        stack = SimpleNamespace(set_visible_child_name=Mock())
        buttons = {p.id: SimpleNamespace(set_active=Mock()) for p in ACTIVITIES}
        manager = SimpleNamespace(state=self.state, stack=stack, buttons=buttons, widgets=retained, closed=False,
            window=SimpleNamespace(sidebar_collapsed=False, save_ui=Mock()))
        manager._display = lambda: SidebarManager._display(manager)
        for _ in range(50):
            for identity in ('source_control', 'transfers', 'devices', 'explorer'):
                SidebarManager.select(manager, identity)
                self.assertEqual(self.state.active, identity)
                self.assertEqual(sum(button.set_active.call_args.args[0] for button in buttons.values()), 1)
        self.assertIs(manager.widgets, retained)
        self.assertEqual(stack.set_visible_child_name.call_count, 200)

    def test_sidebar_zoom_context_precedence_and_terminal_safety(self):
        calls = []
        registry = CommandRegistry()
        registry.register(Command('view.zoom_in', 'Zoom', 'General', 'global', lambda: calls.append('main'), 'Ctrl+='))
        registry.register(Command('sidebar.zoom_in', 'Sidebar Zoom', 'Views', 'sidebar', lambda: calls.append('sidebar'), 'Ctrl+='))
        registry.dispatch('Ctrl+=', 'file_tree'); registry.dispatch('Ctrl+=', 'sidebar')
        registry.dispatch('Ctrl+=', 'editor'); registry.dispatch('Ctrl+=', 'terminal')
        self.assertEqual(calls, ['sidebar', 'sidebar', 'main', 'main'])
        for shortcut in ('Ctrl+C', 'Ctrl+D', 'Ctrl+Z'): self.assertFalse(registry.dispatch(shortcut, 'terminal'))
        registry.set('sidebar.zoom_in', 'F8')
        self.assertEqual(registry.binding('sidebar.zoom_in'), 'F8')

    def test_registered_activity_commands_and_existing_defaults(self):
        registry = CommandRegistry(); opened = []
        window = SimpleNamespace(current_editor=lambda: None, show_sidebar_panel=opened.append)
        register_core(window, registry)
        for shortcut, identity in [('Ctrl+Shift+E', 'explorer'), ('Ctrl+Shift+F', 'search'), ('Ctrl+Shift+G', 'source_control')]:
            self.assertTrue(registry.dispatch(shortcut, 'global'))
            self.assertEqual(opened[-1], identity)
        self.assertEqual(registry.binding('sidebar.toggle'), 'Ctrl+B')
        self.assertEqual(registry.binding('terminal.new'), 'Ctrl+Shift+T')
        self.assertFalse(any(len(ids) > 1 for ids in registry._groups.values()))

    def test_focused_sidebar_does_not_inherit_active_git_tab_context(self):
        class Node:
            def __init__(self, parent=None): self.parent = parent
            def is_ancestor(self, target): return self.parent is target
        sidebar, workspace = Node(), Node()
        page = SimpleNamespace(keybinding_context='git')
        window = SimpleNamespace(get_focus=lambda: Node(sidebar), workspace_sidebar=workspace, left_sidebar=sidebar,
            current_editor=lambda: None, notebook=SimpleNamespace(get_nth_page=lambda _: page, get_current_page=lambda: 0))
        self.assertEqual(focused_context(window), 'sidebar')

    def test_public_plugin_badge_and_focus_use_panel_identity(self):
        api = SimpleNamespace(require=Mock(), plugin_id='example-plugin')
        section = SimpleNamespace(set_expanded=Mock())
        api._host = SimpleNamespace(sidebar_sections={('example-plugin', 'info'): section}, plugin_sidebar_badge=Mock(),
            show_sidebar_panel=Mock(), plugin_panel_id=NauiWindow.plugin_panel_id)
        ui = UIAPI(api)
        ui.focus_sidebar('info'); ui.set_sidebar_badge('info', 7)
        self.assertEqual(api._host.show_sidebar_panel.call_args.args[0], NauiWindow.plugin_panel_id('example-plugin', 'info'))
        api._host.plugin_sidebar_badge.assert_called_once_with('example-plugin', 'info', 7)
        with self.assertRaises(ValueError): ui.set_sidebar_badge('missing', 1)
        with self.assertRaises(ValueError): ui.set_sidebar_badge('info', True)
        self.assertNotEqual(NauiWindow.plugin_panel_id('example-plugin', 'info'), NauiWindow.plugin_panel_id('another-plugin', 'info'))

    def test_malformed_plugin_metadata_is_rejected_before_native_panel_creation(self):
        host = SimpleNamespace(plugin_sidebar=Mock())
        api = SimpleNamespace(require=Mock(), _host=host, plugin_id='example-plugin', _rows=lambda rows: rows)
        for identity, title in [(None, 'Title'), ('', 'Title'), ('info', None), ('info', ''), ('info', 'x' * 201)]:
            with self.assertRaises(ValueError): PluginAPI.sidebar(api, identity, title, [])
        host.plugin_sidebar.assert_not_called()
        PluginAPI.sidebar(api, 'monitor', 'Monitor', [{'text': 'Ready'}])
        host.plugin_sidebar.assert_called_once_with('example-plugin', 'monitor', 'Monitor', [{'text': 'Ready'}])

    def test_sidebar_css_parses_at_requested_scales_without_theme_warnings(self):
        for scale in (80, 100, 125, 150):
            for density in ('compact', 'comfortable'):
                self.settings.values.update(sidebar_scale=scale, sidebar_density=density)
                provider = Gtk.CssProvider(); errors = []
                provider.connect('parsing-error', lambda _, section, error: errors.append(str(error)))
                host = SimpleNamespace(window=SimpleNamespace(ui_settings=self.settings), css=provider,
                    images={}, rail_scroll=SimpleNamespace(set_size_request=Mock()))
                SidebarManager.apply_style(host)
                self.assertFalse(errors, errors)

    def test_settings_factory_uses_native_widget_interfaces_and_one_vertical_scroller(self):
        # Native constructor interfaces, mocked because a display is unavailable.
        # In particular a ScrolledWindow cannot accidentally be used as a Box.
        boxes, scrolls = [], []
        def box(**kwargs):
            widget = MagicMock(spec=Gtk.Box); boxes.append(widget); return widget
        def scroll(**kwargs):
            widget = MagicMock(spec=Gtk.ScrolledWindow); scrolls.append(widget); return widget
        fake = SimpleNamespace(Box=box, ScrolledWindow=scroll, Orientation=Gtk.Orientation,
            PolicyType=Gtk.PolicyType, AccessibleProperty=Gtk.AccessibleProperty,
            Label=lambda **kw: MagicMock(spec=Gtk.Label), Button=lambda **kw: MagicMock(spec=Gtk.Button),
            ComboBoxText=lambda: MagicMock(spec=Gtk.ComboBoxText),
            SpinButton=SimpleNamespace(new_with_range=lambda *args: MagicMock(spec=Gtk.SpinButton)))
        window = SimpleNamespace(ui_settings=self.settings, show_keybindings=Mock(), show_plugins=Mock(), reset_layout=Mock())
        manager = SimpleNamespace(signals=SimpleNamespace(connect=Mock()), scale=Mock())
        with patch('naui.sidebar.Gtk', fake): result = settings_panel(window, manager)
        self.assertIs(result, scrolls[0])
        self.assertEqual(len(boxes), 1)
        self.assertEqual(len(scrolls), 1)
        result.set_child.assert_called_once_with(boxes[0])
        result.set_policy.assert_called_once_with(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    def test_global_zoom_from_sidebar_entry_leaves_main_fonts_unchanged(self):
        sidebar = SimpleNamespace(zoom=Mock())
        focus = SimpleNamespace(is_ancestor=lambda parent: parent is sidebar)
        window = SimpleNamespace(_shutting_down=False, get_focus=lambda: focus, left_sidebar=sidebar, ui_settings=self.settings)
        NauiWindow.zoom_active(window, 1)
        sidebar.zoom.assert_called_once_with(1)
        self.assertEqual((self.settings.values['terminal_font'], self.settings.values['editor_font']), (12, 12))


if __name__ == '__main__': unittest.main()

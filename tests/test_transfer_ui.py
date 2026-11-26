"""Headless native GTK DND controllers and opaque-token routing to core jobs."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from naui.transfers import FileLocation, TransferManager
from naui.workspace import Workspace
from test_transfers import wait_for
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gdk', '4.0')
    from naui.transfer_ui import DragBroker, TransferPanel, human_bytes
    from naui.lifecycle import Signals
    from naui.plugin_api import PluginAPI
    from gi.repository import Gdk
except (ImportError, ValueError):
    DragBroker = None


@unittest.skipIf(DragBroker is None, 'GTK bindings unavailable')
class TransferDragTests(unittest.TestCase):
    def test_tokens_cannot_be_forged_reused_or_used_after_expiry(self):
        broker = DragBroker()
        location = FileLocation('ssh-origin', '/path with spaces')
        token = broker.create(location)
        self.assertIsNone(broker.consume('naui-file:forged'))
        self.assertEqual(broker.consume(token), location)
        self.assertIsNone(broker.consume(token))
        with patch('naui.transfer_ui.time.monotonic', return_value=1):
            token = broker.create(location)
        with patch('naui.transfer_ui.time.monotonic', return_value=602):
            self.assertIsNone(broker.consume(token))
        broker.create(location); broker.close()
        self.assertFalse(broker.tokens)

    def test_native_dnd_handlers_queue_a_copy_via_plugin_transfer_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(directory)
            source = root / 'test õ.txt'; source.write_text('dragged')
            target = root / 'target'; target.mkdir()
            host = SimpleNamespace(_shutting_down=False, workspace=workspace)
            manager = host.transfers = TransferManager(workspace.devices.__getitem__)
            api = PluginAPI(host, 'base-file-explorer', {'filesystem_read', 'filesystem_write', 'file_transfer'}, bundled=True)
            host.plugins = SimpleNamespace(features={'explorer_transfers': api.transfers})
            host.set_status = lambda message: self.fail(message)
            device = workspace.devices['local']
            panel = SimpleNamespace(closed=False, window=host, drag_broker=DragBroker(), set_expanded=lambda value: None)
            explorer = SimpleNamespace(_shutting_down=False, device=device)
            callbacks = {}
            class RecordingSignals(Signals):
                def connect(self, obj, name, callback):
                    callbacks[(obj, name)] = callback
                    return super().connect(obj, name, callback)
            signals = RecordingSignals()
            controls = []
            widget = SimpleNamespace(add_controller=controls.append)
            node = [SimpleNamespace(entry=device.filesystem_backend.properties(str(source)), inline=None)]
            item = SimpleNamespace(get_item=lambda: SimpleNamespace(get_item=lambda: node[0]))
            try:
                TransferPanel.attach_row(panel, widget, item, explorer, signals)
                drag, drop = controls
                provider = callbacks[(drag, 'prepare')](drag, 0, 0)
                self.assertIsInstance(provider, Gdk.ContentProvider)
                token = drag._token
                node[0] = SimpleNamespace(entry=device.filesystem_backend.properties(str(target)), inline=None)
                self.assertTrue(callbacks[(drop, 'drop')](drop, token, 0, 0))
                wait_for(lambda: not manager.active())
                self.assertEqual((target / source.name).read_text(), 'dragged')
                self.assertTrue(source.exists())
                self.assertFalse(callbacks[(drop, 'drop')](drop, token, 0, 0))
                self.assertFalse(callbacks[(drop, 'drop')](drop, '/etc/passwd', 0, 0))
            finally:
                signals.close()
                manager.close(); manager.join()
                panel.drag_broker.close()

    def test_human_byte_units(self):
        self.assertEqual(human_bytes(1024), '1.0 KiB')

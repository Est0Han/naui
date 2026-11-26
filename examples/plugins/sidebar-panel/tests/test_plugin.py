import unittest
from types import SimpleNamespace
from plugin import Plugin


class PanelTests(unittest.TestCase):
    def test_model_and_action(self):
        panels, messages = [], []
        api = SimpleNamespace(ui=SimpleNamespace(sidebar=lambda *args: panels.append(args)),
                              notifications=SimpleNamespace(show=messages.append))
        plugin = Plugin(); plugin.activate(api)
        panels[0][2][0]['actions'][0][1]()
        self.assertEqual(messages, ['Panel action'])
        plugin.deactivate(); plugin.deactivate()

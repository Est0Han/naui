import unittest
from types import SimpleNamespace
from plugin import Plugin


class TerminalTests(unittest.TestCase):
    def test_selected_remote_device_routes_through_terminal_api(self):
        commands, runs = {}, []
        device = SimpleNamespace(id='remote-test', current_path='/tmp/a b')
        api = SimpleNamespace(commands=SimpleNamespace(add=lambda key, title, callback: commands.update({key: callback})),
            devices=SimpleNamespace(selected=lambda: device), terminals=SimpleNamespace(run=lambda **kwargs: runs.append(kwargs)))
        Plugin().activate(api); commands['pwd']()
        self.assertEqual(runs, [dict(device_id='remote-test', cwd='/tmp/a b', command=['pwd'], new_terminal=True)])

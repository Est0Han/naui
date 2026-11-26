import unittest
from types import SimpleNamespace
from plugin import Plugin


class HelloTests(unittest.TestCase):
    def test_command_registration_and_notification(self):
        commands, messages = {}, []
        api = SimpleNamespace(commands=SimpleNamespace(add=lambda key, title, callback: commands.update({key: callback})),
                              notifications=SimpleNamespace(show=messages.append))
        plugin = Plugin(); plugin.activate(api)
        commands['hello']()
        self.assertEqual(messages, ['Hello from Naui!'])
        plugin.deactivate(); plugin.deactivate()

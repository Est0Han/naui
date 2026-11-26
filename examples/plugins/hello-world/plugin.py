from naui_plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def activate(self, api):
        api.commands.add('hello', 'Hello World', lambda: api.notifications.show('Hello from Naui!'))

    def deactivate(self): pass

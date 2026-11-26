from naui_plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def activate(self, api):
        api.ui.sidebar('example', 'EXAMPLE PLUGIN', [
            {'text': 'A panel without owning native widgets',
             'actions': [('Hello', lambda: api.notifications.show('Panel action'))]}])

    def deactivate(self): pass

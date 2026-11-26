from naui_plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def activate(self, api):
        def inspect(location, entry):
            api.files.read(location, lambda text, error: api.notifications.show(
                str(error) if error else f'{location.device_id}: {len(text)} characters'))
        api.files.add_action('inspect', 'Inspect Text', lambda entry: entry.kind == 'f', inspect)

    def deactivate(self): pass

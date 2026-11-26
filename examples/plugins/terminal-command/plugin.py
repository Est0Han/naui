from naui_plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def activate(self, api):
        def run():
            device = api.devices.selected()
            if device:
                api.terminals.run(device_id=device.id, cwd=device.current_path,
                                  command=['pwd'], new_terminal=True)
        api.commands.add('pwd', 'Print Device Directory', run)

    def deactivate(self): pass

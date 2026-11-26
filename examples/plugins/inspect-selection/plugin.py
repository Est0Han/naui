"""Public API only. No GTK internals, SSH commands or credentials."""

def start(api):
    def inspect():
        location = api.selected_location()
        if location is None:
            api.message('Select a file or folder first.')
            return
        def done(info, error):
            api.message(str(error) if error else
                        f'{location.device_id}:{location.path} — {info.size} bytes, type {info.kind}')
        api.properties(location, done)
    api.add_command('inspect', 'Inspect selected file/folder', inspect)

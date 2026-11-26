from naui_plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def activate(self, api):
        self.api = api
        self.messages = 'Panel demo. Messages below are local echoes, not AI responses.'
        if not getattr(api.ui, 'supports', lambda _: False)('right_panels'):
            raise RuntimeError('PLUGIN_API_CAPABILITY_MISSING: Update NaUi for right-side panels.')
        api.commands.add('show', 'Show Example Right Panel', lambda: api.ui.focus_panel('chat'))
        self.render()

    def render(self):
        self.api.ui.panel('chat', 'Example', text=self.messages,
                          rows=[{'text': 'Local demo', 'actions': [('Clear', self.clear)]}],
                          on_submit=self.message, placeholder='Type a message to test the panel')

    def message(self, text):
        # A real agent plugin would use documented async command/network APIs.
        # Keep messages bounded; never block the UI or scrape terminal output.
        self.messages = (self.messages + '\n\nYou: ' + text)[-32000:]
        self.render()

    def clear(self):
        self.messages = 'Conversation cleared.'
        self.render()

    def deactivate(self):
        self.api = None  # NaUi removes the panel and cancels owned callbacks.

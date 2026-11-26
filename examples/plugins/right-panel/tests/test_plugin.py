import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

class PanelExampleTests(TestCase):
    def test_publish_and_message(self):
        spec=importlib.util.spec_from_file_location('panel_example',Path(__file__).parents[1]/'plugin.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        plugin=module.Plugin();api=Mock();api.ui.supports.return_value=True
        plugin.activate(api)
        api.ui.panel.assert_called_once()
        plugin.message('hello')
        self.assertIn('You: hello',api.ui.panel.call_args.kwargs['text'])
        plugin.deactivate();self.assertIsNone(plugin.api)

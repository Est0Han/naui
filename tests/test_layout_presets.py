"""Bounded, private layout persistence without editor/session data."""
from pathlib import Path
import tempfile
import unittest
import gi
gi.require_version('Gtk','4.0'); gi.require_version('Gdk','4.0')
from naui.layout_presets import PresetStore
from naui.ui_settings import UISettings

class LayoutPresetTests(unittest.TestCase):
    def test_save_rename_update_delete_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'layouts.json';store=PresetStore(path)
            value={'layout':'vertical','settings':{'sidebar_width':420,'explorer_device_heights':{'local':480},'split_ratio':650}}
            store.save('Coding',value)
            value['settings']['sidebar_width']=200
            self.assertEqual(store.presets['Coding']['settings']['sidebar_width'],420)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            store=PresetStore(path);store.rename('Coding','Review')
            store.save('Review',value,replace=True)
            self.assertEqual(PresetStore(path).presets['Review']['settings']['sidebar_width'],200)
            store.delete('Review');self.assertEqual(PresetStore(path).presets,{})
    def test_reject_names_nonpresentation_data_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as root:
            store=PresetStore(Path(root)/'layouts.json');value={'layout':'single','settings':{}}
            for name in ('','a\nb','x'*61):
                with self.assertRaises(ValueError):store.save(name,value)
            store.save('One',value);store.save('Two',value)
            with self.assertRaises(ValueError):store.rename('One','Two')
            with self.assertRaises(ValueError):store.save('One',value)
            with self.assertRaises(ValueError):store.save('Bad',{'layout':'single','settings':{'selected_device':'local'}})
    def test_invalid_files_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'layouts.json';path.write_text('broken')
            store=PresetStore(path);self.assertFalse(store.valid)
            with self.assertRaises(ValueError):store.save('New',{'layout':'single','settings':{}})
            self.assertEqual(path.read_text(),'broken')
    def test_legacy_ui_and_new_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'ui.json';path.write_text('{"sidebar_width":300}')
            ui=UISettings(path);self.assertTrue(ui.valid)
            ui.save(explorer_device_heights={'local':1200},split_ratio=700)
            self.assertEqual(UISettings(path).values['explorer_device_heights'],{'local':1200})
            for settings in ({'split_ratio':999},{'explorer_device_heights':{'local':1201}}, {'explorer_device_heights':{'local':True}}):
                with self.assertRaises(ValueError):UISettings.validate(settings)

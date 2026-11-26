import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from naui.installation import InstallPaths, desktop_exec, install, uninstall
from naui.metadata import version_string

ROOT = Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.paths = InstallPaths.make(Path(self.directory.name) / "user prefix õ")
        self.refresh = patch("naui.installation.refresh_desktop")
        self.refresh.start()
        self.addCleanup(self.refresh.stop)

    def test_twice_installed_launcher_works_and_does_not_depend_on_checkout(self):
        launcher = install(ROOT, self.paths)
        manifest = self.paths.manifest.read_bytes()
        install(ROOT, self.paths)
        self.assertEqual(self.paths.manifest.read_bytes(), manifest)
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        result = subprocess.run([str(launcher), "--version"], cwd=self.directory.name,
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), version_string())
        entry = self.paths.prefix / "share/applications/naui.desktop"
        self.assertIn(desktop_exec(launcher), entry.read_text())
        self.assertTrue((self.paths.prefix / "share/icons/hicolor/scalable/apps/naui.svg").is_file())
        if shutil.which("desktop-file-validate"):
            subprocess.run(["desktop-file-validate", str(entry)], check=True)
        self.assertFalse(list(self.paths.app.rglob("__pycache__")))

    def test_uninstall_preserves_user_config_and_unrelated_files(self):
        install(ROOT, self.paths)
        config = self.paths.app / "config.json"
        config.write_text('{"mine":true}')
        unrelated = self.paths.prefix / "bin/another-application"
        unrelated.write_text("keep me")
        self.assertEqual(uninstall(self.paths), [])
        self.assertEqual(config.read_text(), '{"mine":true}')
        self.assertEqual(unrelated.read_text(), "keep me")
        self.assertFalse(self.paths.launcher.exists())
        self.assertFalse(self.paths.manifest.exists())
        self.assertEqual(uninstall(self.paths), [])

    def test_installed_plugin_sdk_and_validator_work_without_checkout(self):
        launcher = install(ROOT, self.paths)
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        environment.pop('DISPLAY', None)
        environment.pop('WAYLAND_DISPLAY', None)
        def run(*arguments):
            return subprocess.run([str(launcher), *arguments], cwd=self.directory.name,
                                  env=environment, capture_output=True, text=True, check=True)
        sdk = Path(run('plugin', 'sdk-path').stdout.splitlines()[0])
        self.assertEqual(sdk, self.paths.app/'docs/plugin-development')
        for name in ('AGENT_INSTRUCTIONS.md', 'API_REFERENCE.md', 'UI_GUIDE.md', 'SECURITY.md', 'MANIFEST.md', 'AI_AGENTS.md'):
            self.assertTrue((sdk/name).is_file())
        for name in ('ai_agents.py', 'ai_launch.py', 'ai_agent_ui.py', 'keybindings.py', 'keybinding_host.py', 'keybinding_ui.py'):
            self.assertTrue((self.paths.app/'src/naui'/name).is_file())
        self.assertTrue((self.paths.app/'docs/keybindings.md').is_file())
        example = self.paths.app/'examples/plugins/hello-world'
        result = json.loads(run('plugin', 'validate', str(example), '--json').stdout)
        self.assertFalse(result['errors'], result)
        self.assertTrue(result['executed'])
        self.assertTrue((self.paths.app/'src/naui_plugin_api/__init__.py').is_file())

    def test_modified_installed_file_is_preserved_and_upgrade_refuses(self):
        install(ROOT, self.paths)
        self.paths.launcher.write_text(self.paths.launcher.read_text() + "# user modification\n")
        with self.assertRaisesRegex(ValueError, "modified"):
            install(ROOT, self.paths)
        self.assertIn("bin/naui", uninstall(self.paths))
        self.assertIn("user modification", self.paths.launcher.read_text())
        self.assertTrue(self.paths.manifest.is_file())

    def test_unowned_collision_preflight_leaves_original_and_no_partial_install(self):
        self.paths.launcher.parent.mkdir(parents=True)
        self.paths.launcher.write_text("original unrelated executable")
        with self.assertRaises(ValueError):
            install(ROOT, self.paths)
        self.assertEqual(self.paths.launcher.read_text(), "original unrelated executable")
        self.assertFalse(self.paths.app.exists())

    def test_manifest_path_traversal_and_symlink_are_rejected(self):
        install(ROOT, self.paths)
        data = json.loads(self.paths.manifest.read_text())
        data["files"]["../../outside"] = "0" * 64
        self.paths.manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            uninstall(self.paths)
        self.paths.manifest.write_text(json.dumps({**data, "files": {}}))
        target = self.paths.prefix / "share/applications"
        shutil.rmtree(target)
        target.symlink_to(self.directory.name, target_is_directory=True)
        with self.assertRaises(ValueError):
            install(ROOT, self.paths)

    def test_malformed_manifest_is_rejected_without_deleting_files(self):
        install(ROOT, self.paths)
        for invalid in ("[]", "null", '{"application":"Naui"}'):
            self.paths.manifest.write_text(invalid)
            with self.assertRaises(ValueError):
                uninstall(self.paths)
            self.assertTrue(self.paths.launcher.exists())

    def test_failed_upgrade_rolls_back_content_and_manifest(self):
        install(ROOT, self.paths)
        record = self.paths.manifest.read_bytes()
        before = {path: path.read_bytes() for path in self.paths.prefix.rglob("*") if path.is_file()}
        replace = os.replace
        calls = 0
        def fail_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("simulated failed filesystem replacement")
            replace(source, destination)
        with patch("naui.installation.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated"):
                install(ROOT, self.paths)
        self.assertEqual(self.paths.manifest.read_bytes(), record)
        self.assertEqual({path: path.read_bytes() for path in self.paths.prefix.rglob("*") if path.is_file()}, before)
        self.assertFalse(list(self.paths.prefix.glob(".naui-install-*")))

    def test_installed_uninstaller_works_for_custom_prefix_without_checkout(self):
        install(ROOT, self.paths)
        script = self.paths.app / "uninstall.sh"
        result = subprocess.run([str(script)], cwd=self.directory.name, capture_output=True, text=True, check=True)
        self.assertIn("uninstall complete", result.stdout)
        self.assertFalse(self.paths.launcher.exists())
        self.assertFalse(self.paths.app.exists())

    def test_desktop_path_quotes_special_characters_and_validates(self):
        special = InstallPaths.make(Path(self.directory.name) / "space ' \" $ ` \\ % õ")
        install(ROOT, special)
        result = subprocess.run([str(special.launcher), "--version"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), version_string())
        desktop = special.prefix / "share/applications/naui.desktop"
        if shutil.which("desktop-file-validate"):
            subprocess.run(["desktop-file-validate", str(desktop)], check=True)
        try:
            import gi
            from gi.repository import Gio, GLib
        except ImportError:
            return
        key = GLib.KeyFile()
        key.load_from_file(str(desktop), GLib.KeyFileFlags.NONE)
        _, arguments = GLib.shell_parse_argv(key.get_string("Desktop Entry", "Exec"))
        self.assertEqual(arguments[0], "/bin/sh")
        self.assertEqual(arguments[1].replace("%%", "%"), str(special.launcher))
        self.assertEqual(arguments[2], "%F")
        try:
            gi.require_version("GioUnix", "2.0")
            from gi.repository import GioUnix
            desktop_class = GioUnix.DesktopAppInfo
        except ValueError:
            desktop_class = Gio.DesktopAppInfo
        self.assertIsNotNone(desktop_class.new_from_filename(str(desktop)))

    def test_invalid_prefix_and_macos_install_are_explicit(self):
        with self.assertRaises(ValueError):
            InstallPaths.make("/")
        with self.assertRaises(ValueError):
            InstallPaths.make("/tmp/path\nwith newline")
        from naui.installation import main
        with patch("naui.installation.sys.platform", "darwin"):
            with self.assertRaises(SystemExit) as exit_status:
                main(ROOT, ["install"])
            self.assertEqual(exit_status.exception.code, 1)

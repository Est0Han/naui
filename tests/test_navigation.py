import subprocess
import tempfile
import unittest
from pathlib import Path

from naui.navigation import NavigationHistory, cd_command, child_path, navigation_path


class NavigationTests(unittest.TestCase):
    def test_pathbar_relative_home_and_literal_shell_syntax(self):
        self.assertEqual(navigation_path("../src", "/home/user/project", "/home/user"), "/home/user/src")
        self.assertEqual(navigation_path("~", "/tmp", "/home/user"), "/home/user")
        self.assertEqual(navigation_path("~/my project", "/tmp", "/home/user"), "/home/user/my project")
        self.assertEqual(navigation_path("$(touch x)", "/tmp", "/home/user"), "/tmp/$(touch x)")
        self.assertEqual(navigation_path(" leading trailing ", "/tmp", "/home/user"), "/tmp/ leading trailing ")
        for text in ["", "x\0y", "~other/project"]:
            with self.assertRaises(ValueError):
                navigation_path(text, "/tmp", "/home/user")

    def test_cd_quoting_never_executes_path_text(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ["name ';$(touch NAUI_INJECTED) õ", "controls\n\x01\x7f"]:
                directory = Path(root, name)
                directory.mkdir()
                command = cd_command(str(directory))
                self.assertTrue(command.startswith("cd -- "))
                self.assertNotIn("\x01", command)
                result = subprocess.run(["bash", "-c", command + "printf '%s' \"$PWD\""],
                                        capture_output=True, check=True)
                self.assertEqual(result.stdout.decode(), str(directory))
            self.assertFalse(Path("NAUI_INJECTED").exists())
            self.assertIn("&& command ls", cd_command(root, True))

    def test_history_changes_only_on_ack_and_preserves_forward(self):
        history = NavigationHistory()
        for path in ["/a", "/b", "/c"]:
            history.observe(path)
        self.assertEqual(history.target(-1), "/b")
        self.assertEqual(history.current, "/c")
        history.observe("/b")
        self.assertEqual(history.current, "/b")
        self.assertEqual(history.target(1), "/c")
        history.observe("/c")
        history.target(-1)
        history.observe("/b")
        history.observe("/new")
        self.assertEqual(history.paths, ["/a", "/b", "/new"])
        self.assertIsNone(history.target(1))

    def test_failed_cd_and_cancel_do_not_move_history(self):
        history = NavigationHistory()
        history.observe("/a")
        history.observe("/b")
        history.target(-1)
        history.observe("/b")
        self.assertEqual(history.current, "/b")
        self.assertEqual(history.index, 1)
        history.target(-1)
        history.cancel()
        self.assertEqual(history.index, 1)

    def test_child_name_validation(self):
        self.assertEqual(child_path("/tmp", "name 'õ"), "/tmp/name 'õ")
        for name in ["", ".", "..", "/etc", "sub/file", "bad\0name"]:
            with self.assertRaises(ValueError):
                child_path("/tmp", name)

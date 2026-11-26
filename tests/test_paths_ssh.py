import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from naui.paths import nano_filenames, resolve_path, sudo_command
from naui.ssh import Connection, parse_interactive, ssh_environment, validate_transport_options


class PathTests(unittest.TestCase):
    def test_relative_absolute_and_metacharacters(self):
        self.assertEqual(resolve_path("/tmp/project", "../file", remote=True), "/tmp/file")
        self.assertEqual(resolve_path("/tmp", "/etc/nginx/nginx.conf", remote=True), "/etc/nginx/nginx.conf")
        self.assertEqual(resolve_path("/tmp", "a'; $(touch x)\n.txt", remote=True), "/tmp/a'; $(touch x)\n.txt")

    def test_local_symlink_target(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "file").write_text("text")
            os.symlink("file", Path(root, "link"))
            self.assertEqual(resolve_path(root, "link"), str(Path(root, "file")))

    def test_invalid_paths(self):
        for cwd, path in [("relative", "file"), ("/tmp", ""), ("/tmp", "x\0y")]:
            with self.assertRaises(ValueError):
                resolve_path(cwd, path)

    def test_nano_options(self):
        self.assertEqual(nano_filenames(["--", "-file"]), ["-file"])
        self.assertEqual(nano_filenames(["one", "two"]), ["one", "two"])
        for args in [[], ["-w", "file"], ["+30", "file"]]:
            with self.assertRaises(ValueError):
                nano_filenames(args)

    def test_sudo_command_detection(self):
        for args in [["nano", "file"], ["-n", "-u", "root", "nano", "file"],
                     ["--user=root", "--", "nano", "file"], ["FOO=1", "nano", "file"]]:
            self.assertEqual(sudo_command(args), "nano")
        for args in [["echo", "nano"], ["-u", "nano", "echo", "nano"], ["--", "/usr/bin/nano", "file"]]:
            self.assertNotEqual(sudo_command(args), "nano")


class SshTests(unittest.TestCase):
    def test_supported_transport(self):
        connection = parse_interactive(["-p2222", "-i", "/tmp/key with spaces", "-J", "jump", "user@alias"])
        self.assertEqual(connection.host, "user@alias")
        self.assertEqual(connection.username, "user")
        self.assertEqual(connection.options, ("-p", "2222", "-i", "/tmp/key with spaces", "-J", "jump"))
        validate_transport_options(list(connection.options))
        self.assertEqual(parse_interactive(["-l", "bob", "alias"]).username, "bob")

    def test_passthrough_forms(self):
        for args in [["alias", "ls"], ["-N", "alias"], ["-T", "alias"], ["-W", "host:22", "jump"],
                     ["-L", "2222:host:22", "alias"], ["-V"], ["-p"], ["--", "-bad"]]:
            self.assertIsNone(parse_interactive(args), args)

    def test_command_no_local_shell_and_config_retained(self):
        command = Connection("alias", ("-F", "/tmp/config")).command("cat -- '/tmp/a b'", batch=True)
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[-2:], ["alias", "cat -- '/tmp/a b'"])
        self.assertIn("BatchMode=yes", command)
        self.assertIn("/tmp/config", command)

    def test_reject_transport_option_injection(self):
        for options in [["--", "evil"], ["-p"], ["-W", "a"], ["-unknown"]]:
            with self.assertRaises(ValueError):
                validate_transport_options(options)

    def test_local_protocol_secrets_never_reach_ssh_environment(self):
        with patch.dict(os.environ, {"NAUI_LOCAL_KEY": "secret", "NAUI_TOKEN": "token",
                                     "SSH_AUTH_SOCK": "/run/agent/socket"}):
            environment = ssh_environment()
            self.assertNotIn("NAUI_LOCAL_KEY", environment)
            self.assertNotIn("NAUI_TOKEN", environment)
            self.assertEqual(environment["SSH_AUTH_SOCK"], "/run/agent/socket")

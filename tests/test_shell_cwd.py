"""Actual prompt/busy reports over real local and simulated remote Bash PTYs."""
import os
from pathlib import Path
import selectors
import tempfile
import time
import unittest

from naui.integration import local_bash_hooks, local_functions, remote_command
from naui.navigation import PREPARE_NAVIGATION, cd_command
from naui.protocol import OscParser, new_token, verify_signature
from naui.pty_bridge import PtyBridge


class ShellHarness:
    def __init__(self, root, remote=False):
        self.token, self.key = new_token(), new_token()
        self.events, self.errors, self.output = [], [], bytearray()
        self.parser = OscParser(self.token, self.events.append, self.errors.append)
        env = dict(os.environ, NAUI_TOKEN=self.token, NAUI_LOCAL_KEY=self.key,
                   TERM="xterm-256color", PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        if remote:
            command = ["bash", "-c", remote_command(self.token, "remote-test")]
        else:
            rc = Path(root, "rc")
            rc.write_text("PROMPT_COMMAND=('printf ORIGINAL_HOOK\\\\n')\nPS0='USER_PREEXEC'\n"
                          + local_functions() + local_bash_hooks() + "\nPS1='TEST> '\n")
            command = ["bash", "--noprofile", "--rcfile", str(rc), "-i"]
        self.bridge = PtyBridge(command, env, root, self.parser)
        self.selector = selectors.DefaultSelector()
        for fd in (self.bridge.front_master, self.bridge.front_slave, self.bridge.shell_master):
            os.set_blocking(fd, False)
            self.selector.register(fd, selectors.EVENT_READ)
        self.wait(lambda: bool(self.cwds))

    @property
    def cwds(self):
        return [event for event in self.events if event["action"] == "cwd"]

    def send(self, data):
        os.write(self.bridge.front_master, data)

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            for key, _ in self.selector.select(0.02):
                fd = key.fd
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    continue
                if fd == self.bridge.front_master:
                    self.output.extend(data)
                elif fd == self.bridge.front_slave:
                    os.write(self.bridge.shell_master, data)
                else:
                    data = self.parser.feed(data)
                    if data:
                        os.write(self.bridge.front_slave, data)
        raise AssertionError(f"PTY timeout: {bytes(self.output)!r}; errors={self.errors}")

    def command(self, command):
        previous = len(self.cwds)
        self.send(command.encode())
        self.wait(lambda: len(self.cwds) > previous)
        return self.cwds[-1]

    def close(self):
        self.selector.close()
        self.bridge.close()
        self.bridge.process.wait(timeout=3)


class CwdChecks:
    remote = False

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.harness = ShellHarness(self.root, self.remote)
        self.addCleanup(self.harness.close)

    def test_cd_previous_pushd_popd_and_failed_cd(self):
        shell = self.harness
        a = Path(self.root, "naui-test", "a")
        b = a / "b"
        b.mkdir(parents=True)
        self.assertEqual(shell.command(cd_command(str(a.parent)))["path"], str(a.parent))
        self.assertEqual(shell.command("cd a\n")["path"], str(a))
        self.assertEqual(shell.command("cd b\n")["path"], str(b))
        self.assertEqual(shell.command("cd ..\n")["path"], str(a))
        self.assertEqual(shell.command("cd -\n")["path"], str(b))
        self.assertEqual(shell.command("pushd /tmp\n")["path"], "/tmp")
        self.assertEqual(shell.command("popd\n")["path"], str(b))
        self.assertEqual(shell.command("cd /naui-missing-directory-xyz\n")["path"], str(b))
        self.assertFalse(shell.errors)
        for event in shell.cwds:
            self.assertEqual(event["location"], "remote" if self.remote else "local")
            self.assertTrue(event["home"].startswith("/"))
            if self.remote:
                self.assertEqual(event["session"], "remote-test")
            else:
                self.assertTrue(verify_signature(event, shell.key))

    def test_clear_incomplete_input_then_quoted_navigation(self):
        shell = self.harness
        path = Path(self.root, "space ';$(touch NEVER_EXECUTED) õ\n\x01")
        path.mkdir()
        shell.send(b"echo unfinished '")
        count = len(shell.events)
        shell.send(PREPARE_NAVIGATION)
        shell.wait(lambda: any(event["action"] == "navigation-ready" for event in shell.events[count:]))
        self.assertEqual(shell.command(cd_command(str(path)))["path"], str(path))
        self.assertFalse(Path("NEVER_EXECUTED").exists())

    def test_busy_before_running_command_and_return_status_preserved(self):
        shell = self.harness
        count, total = len(shell.cwds), len(shell.events)
        shell.send(b"sleep 0.2\n")
        shell.wait(lambda: any(event["action"] == "busy" for event in shell.events[total:]))
        self.assertEqual(len(shell.cwds), count)
        shell.wait(lambda: len(shell.cwds) > count)
        shell.command("false\n")
        shell.command("printf 'STATUS=%s\\n' \"$?\"\n")
        shell.wait(lambda: b"STATUS=1\r\n" in shell.output)
        if not self.remote:
            self.assertIn(b"ORIGINAL_HOOK", shell.output)
            self.assertIn(b"USER_PREEXEC", shell.output)

    def test_continuation_prompt_reports_busy(self):
        shell = self.harness
        total = len(shell.events)
        shell.send(b"echo 'unfinished\n")
        shell.wait(lambda: any(event["action"] == "busy" for event in shell.events[total:]))
        shell.send(b"'\n")
        shell.wait(lambda: any(event["action"] == "cwd" for event in shell.events[total:]))

    def test_disabled_prompt_expansion_refuses_navigation_readiness(self):
        shell = self.harness
        shell.command("shopt -u promptvars\n")
        total = len(shell.events)
        shell.send(PREPARE_NAVIGATION)
        shell.wait(lambda: b"Naui: Navigation requires Bash promptvars" in shell.output)
        self.assertFalse(any(event["action"] == "navigation-ready" for event in shell.events[total:]))


class LocalCwdTests(CwdChecks, unittest.TestCase):
    pass


class RemoteBashCwdTests(CwdChecks, unittest.TestCase):
    remote = True

import os
from pathlib import Path
import selectors
import shlex
import subprocess
import tempfile
import time
import unittest

from naui.integration import local_bash_hooks, local_functions, remote_command
from naui.protocol import OscParser, new_token, verify_signature
from naui.pty_bridge import PtyBridge


class ShellIntegrationTests(unittest.TestCase):
    def test_remote_bash_json_unicode_and_controls(self):
        token, session = new_token(), "test-session"
        names = ["quote'\"\\õ.py", "line\nfile", "control\x01.txt", "space name"]
        script = "set +o emacs\nset +o vi\nnano " + " ".join(shlex.quote(x) for x in names) + "\nexit\n"
        result = subprocess.run(["bash", "-c", remote_command(token, session)],
                                input=script.encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=5)
        messages, errors = [], []
        parser = OscParser(token, messages.append, errors.append)
        parser.feed(result.stdout)
        self.assertFalse(errors)
        self.assertEqual(messages[0]["action"], "session-ready")
        opens = [message for message in messages if message["action"] == "open"]
        self.assertEqual(len(opens), 1, result.stderr)
        self.assertEqual(opens[0]["args"], names)
        self.assertEqual(opens[0]["location"], "remote")
        self.assertEqual(opens[0]["session"], session)

    def test_remote_sudo_preserves_arguments_without_editor_interception(self):
        # A disposable sudo stand-in: no elevation or system files touched.
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory, 'sudo')
            stub.write_text('#!/bin/bash\nprintf "ARG:%s\\n" "$@"\nexit 17\n')
            stub.chmod(0o700)
            script = 'PATH=' + shlex.quote(directory) + ':"$PATH"\nsudo -n -u root -- nano "space name"\nexit $?\n'
            result = subprocess.run(['bash', '-c', remote_command(new_token(), 'test')],
                                    input=script.encode(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=5)
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertIn(b'ARG:-n\nARG:-u\nARG:root\nARG:--\nARG:nano\nARG:space name\n', result.stdout)
            self.assertNotIn(b'editing is not supported', result.stderr)

    def test_local_sudo_delegates_unchanged(self):
        from unittest.mock import patch
        from naui.shell import main
        args = ['-n', '-u', 'root', '--', 'nano', 'space name']
        with patch.dict(os.environ, NAUI_TOKEN='test', NAUI_LOCAL_KEY='test'), patch('naui.shell.run_child', return_value=17) as run:
            self.assertEqual(main(['sudo', *args]), 17)
            run.assert_called_once_with(['sudo', *args])

    def test_remote_cwd_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root, "cwd\n")
            directory.mkdir()
            token = new_token()
            script = "set +o emacs\ncd " + shlex.quote(str(directory)) + "\nnano file\nexit\n"
            result = subprocess.run(["bash", "-c", remote_command(token, "test")],
                                    input=script.encode(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=5)
            messages = []
            parser = OscParser(token, messages.append)
            parser.feed(result.stdout)
            self.assertEqual([message for message in messages if message["action"] == "open"][-1]["cwd"], str(directory))

    def test_real_double_pty_shell_and_nano_event(self):
        with tempfile.TemporaryDirectory() as directory:
            token, key = new_token(), new_token()
            messages, errors = [], []
            parser = OscParser(token, messages.append, errors.append)
            rc = Path(directory, "rc")
            rc.write_text(local_functions() + "\nexport -f nano ssh sudo\nPS1='NAUI_TEST> '\n")
            env = dict(os.environ, NAUI_TOKEN=token, NAUI_LOCAL_KEY=key, TERM="xterm-256color",
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            bridge = PtyBridge(["/bin/bash", "--noprofile", "--rcfile", str(rc), "-i"], env, directory, parser)
            self.addCleanup(bridge.close)
            self.addCleanup(lambda: bridge.process.wait(timeout=3))
            # This simulates VTE at its REAL master and uses the same two PTYs
            # as the GUI. It proves controlling tty/job control, not GUI layout.
            selector = selectors.DefaultSelector()
            self.addCleanup(selector.close)
            for fd in (bridge.front_master, bridge.front_slave, bridge.shell_master):
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ)
            output = bytearray()

            def pump(until, timeout=5):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if until():
                        return
                    for event, _ in selector.select(0.05):
                        fd = event.fd
                        try:
                            data = os.read(fd, 65536)
                        except (OSError, BlockingIOError):
                            continue
                        if fd == bridge.front_master:
                            output.extend(data)
                        elif fd == bridge.front_slave:
                            os.write(bridge.shell_master, data)
                        else:
                            rendered = parser.feed(data)
                            if rendered:
                                os.write(bridge.front_slave, rendered)
                self.fail(f"PTY timeout: {bytes(output)!r}; messages={messages}; errors={errors}")

            pump(lambda: b"NAUI_TEST> " in output)
            os.write(bridge.front_master, b"echo hello\npwd\ntest -t 0 && echo REAL_TTY\nnano /tmp/naui-test.txt\n")
            pump(lambda: len(messages) == 1 and b"REAL_TTY\r\n" in output)
            self.assertIn(b"hello\r\n", output)
            self.assertIn(directory.encode(), output)
            self.assertEqual(messages[0]["args"], ["/tmp/naui-test.txt"])
            self.assertTrue(verify_signature(messages[0], key))
            self.assertNotIn(b"\x1b]777;naui;", output)
            self.assertFalse(errors)
            # Interactive nested Bash inherits integration, while GNU nano's
            # absolute path is untouched and resolves to the actual executable.
            os.write(bridge.front_master, b"bash --noprofile --norc -ic 'nano nested.py'\n")
            pump(lambda: len(messages) == 2)
            self.assertEqual(messages[1]["args"], ["nested.py"])
            bridge.resize(35, 110)
            os.write(bridge.front_master, b"stty size\n")
            pump(lambda: b"35 110\r\n" in output)
            os.write(bridge.front_master, b"exit\n")
            pump(lambda: bridge.process.poll() is not None)

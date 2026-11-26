"""Nonce/uid broker and session-only credential isolation, no external SSH host."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from naui.authentication import AuthenticationManager, AuthPrompt, packet_receive, packet_send


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError('Timed out waiting for authentication prompt')


class CredentialMemoryTests(unittest.TestCase):
    def setUp(self):
        self.manager = AuthenticationManager()
        self.addCleanup(self.manager.cleanup)

    def test_two_devices_have_separate_session_memory_and_close_discards_it(self):
        a, b = AuthPrompt('a', 'Password:'), AuthPrompt('b', 'Password:')
        self.manager._pending.extend([a, b])
        self.manager.answer(a, 'a-value', remember=True)
        self.manager.answer(b, 'b-value', remember=True)
        self.assertEqual(self.manager._remembered[('a', 'Password:')], 'a-value')
        self.assertEqual(self.manager._remembered[('b', 'Password:')], 'b-value')
        self.assertNotIn('a-value', repr(a))
        self.manager.forget_device('a')
        self.assertNotIn(('a', 'Password:'), self.manager._remembered)
        self.assertIn(('b', 'Password:'), self.manager._remembered)
        self.manager.close(); self.manager.close()
        self.assertFalse(self.manager._remembered)
        self.assertFalse(self.manager.pending())
        self.assertIsNone(a.response)
        self.assertIsNone(b.response)

    def test_host_confirmation_is_never_remembered_and_invalid_response_rejected(self):
        prompt = AuthPrompt('a', 'Are you sure (yes/no)?', True)
        self.manager._pending.append(prompt)
        self.manager.answer(prompt, 'yes', remember=True)
        self.assertFalse(self.manager._remembered)
        for value in ('password\nnext', 'secret\0', 'x' * 4097):
            with self.assertRaises(ValueError):
                self.manager.answer(AuthPrompt('a', 'Password:'), value)

    def test_cancelling_password_prompt_cancels_only_its_transfer(self):
        from types import SimpleNamespace
        condition = threading.Condition()
        control = SimpleNamespace(cancelled=False, condition=condition)
        prompt = AuthPrompt('a', 'Password:', control=control)
        self.manager._pending.append(prompt)
        self.manager.answer(prompt)
        self.assertTrue(control.cancelled)
        self.assertTrue(prompt.done.is_set())
        self.assertIsNone(prompt.response)


class AskpassBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='naui-auth-test-')
        self.addCleanup(self.temp.cleanup)
        self.env_patch = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.temp.name})
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.manager = AuthenticationManager(); self.addCleanup(self.manager.cleanup)
        try:
            self.manager._start()
        except PermissionError as error:
            self.skipTest('Sandbox blocks local Unix socket broker: ' + str(error))

    def helper(self, environment, prompt='Password:'):
        process = subprocess.Popen([environment['SSH_ASKPASS'], prompt], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process

    def test_local_helper_uses_maskable_prompt_not_password_argv_or_environment(self):
        with self.manager.operation('server-a') as environment:
            process = self.helper(environment)
            prompt = wait_for(lambda: self.manager.pending())[0]
            self.assertEqual(prompt.device_id, 'server-a')
            self.assertEqual(environment['SSH_ASKPASS_REQUIRE'], 'force')
            self.assertNotIn('opaque-test-value', json.dumps(environment))
            self.assertFalse(any(key.startswith('NAUI_') for key in environment))
            self.assertNotIn('opaque-test-value', repr(process.args))
            self.manager.answer(prompt, 'opaque-test-value')
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(stdout, b'opaque-test-value\n')
            self.assertEqual(stderr, b'')
        directory = Path(self.manager._directory.name)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'broker').stat().st_mode & 0o777, 0o600)
        self.manager.cleanup()
        self.assertFalse(directory.exists())
        self.assertFalse(self.manager._operations)

    def test_remembered_value_does_not_cross_devices_rejected_cached_value_is_reprompted(self):
        with self.manager.operation('server-a') as a:
            first = self.helper(a)
            prompt = wait_for(lambda: self.manager.pending())[0]
            self.manager.answer(prompt, 'a-session', remember=True)
            self.assertEqual(first.communicate(timeout=3)[0], b'a-session\n')
        with self.manager.operation('server-a') as a:
            reused = self.helper(a)
            self.assertEqual(reused.communicate(timeout=3)[0], b'a-session\n')
            rejected = self.helper(a)
            prompt = wait_for(lambda: self.manager.pending())[0]
            self.assertEqual(prompt.device_id, 'server-a')
            self.manager.answer(prompt, 'replacement')
            self.assertEqual(rejected.communicate(timeout=3)[0], b'replacement\n')
        with self.manager.operation('server-b') as b:
            process = self.helper(b)
            prompt = wait_for(lambda: self.manager.pending())[0]
            self.assertEqual(prompt.device_id, 'server-b')
            self.manager.answer(prompt)
            self.assertEqual(process.communicate(timeout=3)[0], b'')
            self.assertEqual(process.returncode, 1)

    def test_invalid_nonce_and_expired_operation_do_not_prompt_or_disclose(self):
        with self.manager.operation('server-a') as environment:
            (Path(environment['SSH_ASKPASS']).parent/'nonce').write_text('0' * 64)
            process = self.helper(environment)
            self.assertEqual(process.communicate(timeout=4)[0], b'')
            self.assertEqual(process.returncode, 1)
            self.assertFalse(self.manager.pending())
        self.assertFalse(Path(environment['SSH_ASKPASS']).exists())
        self.assertFalse(self.manager._operations)
        self.assertFalse(self.manager.pending())

    def test_close_cancels_pending_helper_and_discards_secrets(self):
        with self.manager.operation('server-a') as environment:
            process = self.helper(environment)
            wait_for(lambda: self.manager.pending())
            self.manager.close()
            self.assertEqual(process.communicate(timeout=4)[0], b'')
            self.assertEqual(process.returncode, 1)
            self.assertFalse(self.manager._remembered)
            self.assertFalse(self.manager._pending)


class MockBrokerConnection:
    """No socket permission needed: exercise the exact broker packet handler."""
    def __init__(self, token, prompt='Password:', uid=None):
        import struct
        self.input = bytearray(json.dumps({'token': token, 'prompt': prompt}).encode() + b'\n')
        self.output = bytearray()
        self.uid = os.getuid() if uid is None else uid
    def settimeout(self, value): pass
    def getsockopt(self, *args):
        import struct
        return struct.pack('3i', 123, self.uid, 123)
    def recv(self, limit):
        result = bytes(self.input[:limit]); del self.input[:limit]; return result
    def sendall(self, data): self.output.extend(data)
    def shutdown(self, how): pass
    def close(self): pass


class MockBrokerTests(unittest.TestCase):
    def setUp(self):
        self.manager = AuthenticationManager()
        self.addCleanup(self.manager.cleanup)
        self.a, self.b = 'a' * 64, 'b' * 64
        self.manager._operations = {self.a: ('server-a', None, set()), self.b: ('server-b', None, set())}

    def run_prompt(self, token, uid=None):
        connection = MockBrokerConnection(token, uid=uid)
        thread = threading.Thread(target=self.manager._answer, args=(connection,))
        self.manager._workers.add(thread)
        self.manager._connections.add(connection)
        thread.start()
        return connection, thread

    def test_valid_nonce_exact_answer_and_cancel_packets(self):
        connection, thread = self.run_prompt(self.a)
        prompt = wait_for(lambda: self.manager.pending())[0]
        self.assertEqual(prompt.device_id, 'server-a')
        self.manager.answer(prompt, 'test-value')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(json.loads(connection.output), {'response': 'test-value'})
        self.assertFalse(self.manager._pending)
        connection, thread = self.run_prompt(self.b)
        self.manager.answer(wait_for(lambda: self.manager.pending())[0])
        thread.join(3)
        self.assertEqual(json.loads(connection.output), {'cancelled': True})

    def test_cached_auth_is_device_scoped_and_rejected_cache_reprompts(self):
        self.manager._remembered[('server-a', 'Password:')] = 'a-only'
        connection, thread = self.run_prompt(self.a); thread.join(3)
        self.assertEqual(json.loads(connection.output), {'response': 'a-only'})
        connection, thread = self.run_prompt(self.b)
        prompt = wait_for(lambda: self.manager.pending())[0]
        self.assertEqual(prompt.device_id, 'server-b')
        self.manager.answer(prompt, 'b-only')
        thread.join(3)
        self.assertEqual(json.loads(connection.output), {'response': 'b-only'})
        connection, thread = self.run_prompt(self.a)
        prompt = wait_for(lambda: self.manager.pending())[0]
        self.assertEqual(prompt.device_id, 'server-a')
        self.assertNotIn(('server-a', 'Password:'), self.manager._remembered)
        self.manager.answer(prompt, 'changed')
        thread.join(3)
        self.assertEqual(json.loads(connection.output), {'response': 'changed'})

    def test_other_uid_or_invalid_nonce_never_get_credentials(self):
        self.manager._remembered[('server-a', 'Password:')] = 'a-only'
        for token, uid in (('z' * 64, None), (self.a, os.getuid() + 1)):
            connection, thread = self.run_prompt(token, uid)
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(connection.output, b'')
            self.assertFalse(self.manager.pending())

    def test_private_operation_nonce_is_not_in_ssh_environment_or_helper_argv(self):
        from types import SimpleNamespace
        self.manager._operations.clear()
        with tempfile.TemporaryDirectory(prefix='naui-auth-context-') as directory:
            self.manager._directory = tempfile.TemporaryDirectory(prefix='broker-', dir=directory)
            socket_path = str(Path(self.manager._directory.name, 'broker'))
            self.manager._socket = SimpleNamespace(getsockname=lambda: socket_path, close=lambda: None)
            self.manager._remembered[('server-a', 'Password:')] = 'never-serialize-this-value'
            with self.manager.operation('server-a') as environment:
                helper = Path(environment['SSH_ASKPASS'])
                nonce = helper.parent/'nonce'
                value = nonce.read_text()
                self.assertIn(value, self.manager._operations)
                self.assertFalse(any(key.startswith('NAUI_') for key in environment))
                self.assertNotIn(value, json.dumps(environment))
                self.assertNotIn(value, helper.read_text())
                self.assertNotIn('never-serialize-this-value', helper.read_text())
                self.assertNotIn('never-serialize-this-value', nonce.read_text())
                self.assertEqual(nonce.stat().st_mode & 0o777, 0o600)
                self.assertEqual(helper.parent.stat().st_mode & 0o777, 0o700)
            self.assertFalse(helper.exists())
            self.assertFalse(self.manager._operations)
            self.manager.cleanup()

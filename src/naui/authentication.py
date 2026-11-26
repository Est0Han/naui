"""Core OpenSSH askpass broker. Credentials never enter plugin APIs or argv.

OpenSSH chooses key/agent/password authentication. Its short-lived LOCAL
askpass helper uses a uid-checked, nonce-authenticated private Unix socket.
Only the application holds prompts/session memory. No TCP, persistence, key
reading or custom SSH authentication. In-process Python cannot guarantee secure
memory erasure; close drops all retained values rather than claiming otherwise.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import struct
import sys
import tempfile
import threading
import time

from .ssh import ssh_environment

MAX_AUTH_PACKET = 16384


@dataclass(eq=False)
class AuthPrompt:
    device_id: str
    prompt: str
    confirmation: bool = False
    control: object = field(default=None, repr=False)
    response: str | None = field(default=None, repr=False)
    remember: bool = False
    done: threading.Event = field(default_factory=threading.Event, repr=False)


def packet_receive(connection):
    data = bytearray()
    while b'\n' not in data:
        chunk = connection.recv(min(4096, MAX_AUTH_PACKET + 1 - len(data)))
        if not chunk:
            raise ValueError('Incomplete authentication message')
        data.extend(chunk)
        if len(data) > MAX_AUTH_PACKET:
            raise ValueError('Authentication message too large')
    return json.loads(bytes(data).split(b'\n', 1)[0])


def packet_send(connection, data):
    packet = json.dumps(data, ensure_ascii=True).encode() + b'\n'
    if len(packet) > MAX_AUTH_PACKET:
        raise ValueError('Authentication response too large')
    connection.sendall(packet)


class AuthenticationManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.closed = False
        self._operations, self._pending, self._remembered = {}, [], {}
        self._directory = None
        self._socket = None
        self._thread = None
        self._workers = set()
        self._connections = set()

    def _start(self):
        with self._lock:
            if self.closed:
                raise RuntimeError('Authentication stopped')
            if self._socket:
                return
            runtime = os.environ.get('XDG_RUNTIME_DIR')
            if runtime and not os.path.isabs(runtime):
                runtime = None
            self._directory = tempfile.TemporaryDirectory(prefix='naui-auth-', dir=runtime)
            try:
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._socket.bind(str(Path(self._directory.name, 'broker')))
                os.chmod(self._socket.getsockname(), 0o600)
                self._socket.listen(8)
                self._socket.settimeout(0.2)
            except OSError:
                if self._socket:
                    self._socket.close()
                self._socket = None
                self._directory.cleanup()
                self._directory = None
                raise
            self._thread = threading.Thread(target=self._serve, name='naui-auth', daemon=True)
            self._thread.start()

    @contextmanager
    def operation(self, device_id, control=None):
        self._start()
        token = secrets.token_hex(32)
        with self._lock:
            if self.closed:
                raise RuntimeError('Authentication stopped')
            address, directory = self._socket.getsockname(), self._directory.name
            if len(self._operations) >= 128:
                raise RuntimeError('Authentication operation queue is full')
            self._operations[token] = (device_id, control, set())
        # The nonce must not be in ssh's environment: a user's SendEnv * could
        # otherwise disclose it to the server. A 0600 local IPC authorization
        # file inside a private operation directory contains only this nonce,
        # never a password. Helpers receive paths, not secrets, in their argv.
        try:
            with tempfile.TemporaryDirectory(prefix='request-', dir=directory) as temporary:
                nonce = Path(temporary, 'nonce')
                nonce.write_text(token)
                nonce.chmod(0o600)
                helper = Path(temporary, 'askpass')
                package_root = str(Path(__file__).resolve().parent.parent)
                bootstrap = ('import sys;sys.path.insert(0,' + repr(package_root)
                             + ');from naui.authentication import askpass_main;raise SystemExit(askpass_main('
                             + repr(str(nonce)) + ',' + repr(address) + '))')
                helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B -c '
                                  + shlex.quote(bootstrap) + ' "$@"\n')
                helper.chmod(0o700)
                environment = ssh_environment()
                environment.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force',
                                   DISPLAY=environment.get('DISPLAY') or 'naui-askpass')
                yield environment
        finally:
            with self._lock:
                self._operations.pop(token, None)

    def _serve(self):
        while not self.closed:
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                if self.closed or len(self._workers) >= 16:
                    connection.close()
                    continue
                worker = threading.Thread(target=self._answer, args=(connection,), name='naui-askpass', daemon=True)
                self._workers.add(worker)
                self._connections.add(connection)
                worker.start()

    def _answer(self, connection):
        request = None
        control = None
        try:
            connection.settimeout(3)
            if hasattr(socket, 'SO_PEERCRED'):
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    return
            payload = packet_receive(connection)
            if not isinstance(payload, dict):
                return
            token, prompt = payload.get('token'), payload.get('prompt')
            if (not isinstance(token, str) or len(token) != 64 or not isinstance(prompt, str)
                    or not prompt or len(prompt.encode()) > 4096):
                return
            with self._lock:
                operation = next((value for key, value in self._operations.items()
                                  if hmac.compare_digest(key, token)), None)
                if operation is None:
                    return
                device_id, control, used = operation
                confirmation = payload.get('hint') == 'confirm' or 'yes/no' in prompt.lower()
                # Reuse at most once for a prompt per operation. Repeated prompts
                # indicate rejection: discard the old value and ask again.
                key = (device_id, prompt)
                remembered = self._remembered.get(key)
                if remembered is not None and key not in used and not confirmation:
                    used.add(key)
                    packet_send(connection, {'response': remembered})
                    return
                if key in used:
                    self._remembered.pop(key, None)
                used.add(key)
                request = AuthPrompt(device_id, prompt, confirmation, control)
                self._pending.append(request)
            if control:
                control.waiting_auth(True)
            deadline = time.monotonic() + 180
            while not request.done.wait(0.1):
                if self.closed or time.monotonic() > deadline:
                    return
                if control:
                    control.check()
                with self._lock:
                    if token not in self._operations:
                        return
            if self.closed or request.response is None:
                packet_send(connection, {'cancelled': True})
            else:
                packet_send(connection, {'response': request.response})
        except (OSError, ValueError, RuntimeError, StopIteration):
            pass
        except Exception:
            # Never log authentication exceptions/payloads or retained secrets.
            pass
        finally:
            if control:
                control.waiting_auth(False)
            with self._lock:
                if request in self._pending:
                    self._pending.remove(request)
                if request:
                    request.response = None
                    request.done.set()
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
            connection.close()

    def pending(self):
        with self._lock:
            return [request for request in self._pending if not request.done.is_set()]

    def answer(self, request, response=None, remember=False):
        if response is not None and (not isinstance(response, str) or '\n' in response or '\r' in response
                or '\0' in response or len(response.encode()) > 4096):
            raise ValueError('Invalid authentication response')
        with self._lock:
            if self.closed or request not in self._pending or request.done.is_set():
                return
            request.response = response
            if response is None and request.control:
                with request.control.condition:
                    request.control.cancelled = True
                    request.control.condition.notify_all()
            if remember and response is not None and not request.confirmation:
                self._remembered[(request.device_id, request.prompt)] = response
            request.done.set()

    def forget_device(self, device_id):
        with self._lock:
            self._remembered = {key: value for key, value in self._remembered.items() if key[0] != device_id}
            tokens = [token for token, value in self._operations.items() if value[0] == device_id]
            for token in tokens:
                del self._operations[token]
            for prompt in self._pending:
                if prompt.device_id == device_id:
                    prompt.response = None
                    prompt.done.set()

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._remembered.clear()
            self._operations.clear()
            for prompt in self._pending:
                prompt.response = None
                prompt.done.set()
            connections, workers = list(self._connections), list(self._workers)
            if self._socket:
                self._socket.close()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=1)
        for worker in workers:
            worker.join(timeout=1)
        with self._lock:
            self._pending.clear()

    def cleanup(self):
        self.close()
        if self._directory:
            self._directory.cleanup()


def askpass_main(token_path, socket_path):
    """OpenSSH's LOCAL one-shot helper; stdout is exclusively its response."""
    try:
        address = socket_path
        token = Path(token_path).read_text()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(185)
            connection.connect(address)
            packet_send(connection, {'token': token, 'prompt': sys.argv[1] if len(sys.argv) > 1 else 'Password:',
                                     'hint': os.environ.get('SSH_ASKPASS_PROMPT', '')})
            result = packet_receive(connection)
            response = result.get('response')
            if not isinstance(response, str) or any(char in response for char in '\n\r\0'):
                return 1
            encoded = (response + '\n').encode('utf-8')
            while encoded:
                encoded = encoded[os.write(sys.stdout.fileno(), encoded):]
            return 0
    except (OSError, ValueError, KeyError):
        return 1

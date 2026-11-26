"""Bounded stdio transport. No GTK, credential files, terminal scraping or model calls on connect."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

MAX_FRAME = 2 * 1024 * 1024


class Client:
    def __init__(self, cwd, command=None):
        if not isinstance(command, (list, tuple)) or not command or any(not isinstance(a,str) or '\0' in a for a in command):
            raise ValueError('An explicit command argument list is required')
        self.incoming, self.outgoing = queue.Queue(128), queue.Queue(64)
        self.stopped = threading.Event()
        self.failed = False
        self.pending, self.sequence = {}, 0
        self.process = subprocess.Popen(command,
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.reader.start(); self.writer.start()

    def _read(self):
        try:
            while not self.stopped.is_set():
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line: break
                if len(line) > MAX_FRAME: raise ValueError('Oversized protocol frame')
                value = json.loads(line)
                if not isinstance(value, dict): raise ValueError('Invalid protocol frame')
                while not self.stopped.is_set():
                    try:
                        self.incoming.put(value, timeout=.1); break
                    except queue.Full: pass
        except (OSError, ValueError): pass
        finally:
            self.failed = True

    def _write(self):
        try:
            while not self.stopped.is_set():
                try: frame = self.outgoing.get(timeout=.1)
                except queue.Empty: continue
                self.process.stdin.write(frame); self.process.stdin.flush()
        except (OSError, ValueError): self.failed = True

    def send(self, message):
        if self.stopped.is_set() or self.failed: raise RuntimeError('Process connection closed. Reconnect to continue.')
        frame = (json.dumps(message, ensure_ascii=True) + '\n').encode()
        if len(frame) > MAX_FRAME: raise ValueError('Message too large')
        try: self.outgoing.put_nowait(frame)
        except queue.Full: raise RuntimeError('Process is not responding. Reconnect.') from None

    def request(self, method, params, callback):
        if len(self.pending) >= 32: raise RuntimeError('Too many pending requests')
        self.sequence += 1
        identity = self.sequence
        self.send({'id': identity, 'method': method, 'params': params})
        self.pending[identity] = (time.monotonic() + 60, callback)

    def drain(self, event):
        for _ in range(64):
            try: message = self.incoming.get_nowait()
            except queue.Empty: break
            if 'method' not in message and 'id' in message:
                pending = self.pending.pop(message['id'], None)
                if pending:
                    # Never expose raw provider error data or auth payloads in logs.
                    pending[1](message.get('result'), 'Process rejected the request.' if 'error' in message else None)
            else: event(message)
        now = time.monotonic()
        for identity, (deadline, callback) in list(self.pending.items()):
            if now > deadline:
                self.pending.pop(identity, None); callback(None, 'Process request timed out. Reconnect before retrying.')
        if self.failed and self.incoming.empty(): raise RuntimeError('Process connection ended. Reconnect to continue.')

    def close(self):
        if self.stopped.is_set(): return
        self.stopped.set(); self.pending.clear()
        # Closing a plugin must not leave its subprocess working invisibly.
        def reap():
            import signal
            try: os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                self.process.wait()
            self.reader.join(timeout=1); self.writer.join(timeout=1)
            for stream in (self.process.stdin, self.process.stdout):
                try: stream.close()
                except OSError: pass
        threading.Thread(target=reap, daemon=True).start()

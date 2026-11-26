"""Local, bounded JSON-RPC/stdio transport. No GTK objects on these threads.

Reader/writer threads only exchange plain dictionaries and Futures. UI code
drains notifications on its main loop. Servers cannot run commands, apply edits
or open links through unsolicited server requests.
"""
from collections import deque
from concurrent.futures import Future, TimeoutError
import json
import os
import queue
import signal
import subprocess
import threading

MAX_MESSAGE = 16 * 1024 * 1024
MAX_NOTIFICATIONS = 4 * 1024 * 1024


class LspError(RuntimeError):
    pass


def encode_message(message):
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise LspError("Language-server message exceeds 16 MiB")
    return b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n" + data


def read_message(stream):
    headers, size = {}, 0
    while True:
        line = stream.readline(8193)
        if not line and not size:
            return None
        size += len(line)
        if not line or size > 8192 or not line.endswith(b"\r\n"):
            raise LspError("Invalid language-server framing")
        if line == b"\r\n":
            break
        try:
            name, value = line[:-2].split(b":", 1)
            key = name.strip().lower()
            if key in headers:
                raise ValueError("Duplicate header")
            headers[key] = value.strip()
        except ValueError as error:
            raise LspError("Invalid language-server header") from error
    length = headers.get(b"content-length", b"")
    if not length.isdigit() or len(length) > 10 or not 0 < int(length) <= MAX_MESSAGE:
        raise LspError("Invalid language-server Content-Length")
    for item in headers.get(b"content-type", b"").lower().split(b";"):
        if item.strip().startswith(b"charset=") and item.strip().split(b"=", 1)[1] not in {b"utf-8", b"utf8"}:
            raise LspError("Language-server content must use UTF-8")
    remaining, chunks = int(length), []
    while remaining:
        data = stream.read(remaining)
        if not data:
            raise LspError("Language-server message was truncated")
        chunks.append(data)
        remaining -= len(data)
    try:
        result = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise LspError("Invalid language-server JSON") from error
    if not isinstance(result, dict) or result.get("jsonrpc") != "2.0":
        raise LspError("Invalid language-server JSON-RPC message")
    if "method" in result and not isinstance(result["method"], str):
        raise LspError("Invalid language-server method")
    if "id" in result and type(result["id"]) not in {int, str, type(None)}:
        raise LspError("Invalid language-server request ID")
    return result




class LspClient:
    def __init__(self, command, cwd, settings=None):
        self.command, self.cwd, self.settings = list(command), str(cwd), settings or {}
        self._lock, self._close_lock = threading.RLock(), threading.Lock()
        self._closed = False
        self._failed = None
        self._next_id = 0
        self._pending = {}
        self._messages = deque()
        self._message_bytes = 0
        self._output = queue.Queue(maxsize=8)
        self._queued_bytes = 0
        self.process = None
        self._threads = []
        self.capabilities = {}
        self.display_text = str

    def start(self, root_uri):
        self.root_uri = root_uri
        with self._lock:
            if self._closed:
                raise LspError("Language server has stopped")
            env = {key: value for key, value in os.environ.items() if not key.startswith(("NAUI_", "__NAUI_"))}
            self.process = subprocess.Popen(self.command, cwd=self.cwd, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
            for target in (self._reader, self._writer, self._stderr):
                thread = threading.Thread(target=target, name="naui-lsp", daemon=True)
                self._threads.append(thread)
                thread.start()
        try:
            result = self.request("initialize", {
                "processId": os.getpid(), "clientInfo": {"name": "Naui"},
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": "Naui workspace"}],
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "workspace": {"configuration": True, "workspaceFolders": True, "applyEdit": False,
                                  "workspaceEdit": {"documentChanges": True, "resourceOperations": []}},
                    "textDocument": {"synchronization": {"didSave": True},
                        "completion": {"completionItem": {"snippetSupport": False}},
                        "codeAction": {"codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}}},
                        "definition": {"linkSupport": True}, "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "publishDiagnostics": {"versionSupport": True}, "rename": {"prepareSupport": False}},
                },
            }, timeout=20)
            if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
                raise LspError("Invalid language-server initialization")
            self.capabilities = result["capabilities"]
            if self.capabilities.get("positionEncoding", "utf-16") != "utf-16":
                raise LspError("Language server requires an unsupported position encoding")
            self.notify("initialized", {})
            self.notify("workspace/didChangeConfiguration", {"settings": self.settings})
        except BaseException:
            self.close()
            raise
        return self

    def _send(self, message):
        packet = encode_message({"jsonrpc": "2.0", **message})
        with self._lock:
            if self._closed or self._failed:
                raise LspError(self._failed or "Language server has stopped")
            if self._queued_bytes + len(packet) > 32 * 1024 * 1024:
                raise LspError("Language-server input queue is full")
            try:
                self._output.put_nowait(packet)
            except queue.Full as error:
                raise LspError("Language-server input queue is full") from error
            self._queued_bytes += len(packet)

    def notify(self, method, params):
        self._send({"method": method, "params": params})

    def request(self, method, params, timeout=12):
        with self._lock:
            self._next_id += 1
            identity = self._next_id
            future = Future()
            self._pending[identity] = future
        try:
            self._send({"id": identity, "method": method, "params": params})
            return future.result(timeout=timeout)
        except TimeoutError as error:
            try:
                self.notify("$/cancelRequest", {"id": identity})
            except LspError:
                pass
            raise LspError("Language-server request timed out") from error
        finally:
            with self._lock:
                self._pending.pop(identity, None)

    def _reader(self):
        try:
            while True:
                message = read_message(self.process.stdout)
                if message is None:
                    raise LspError("Language server exited; the editor remains usable")
                if "method" in message:
                    if "id" in message:
                        self._server_request(message)
                    else:
                        self._enqueue(message)
                elif "id" in message:
                    identity = message["id"]
                    if type(identity) is not int:
                        continue
                    with self._lock:
                        future = self._pending.get(identity)
                        if future is not None and not future.done():
                            if "error" in message:
                                detail = message["error"]
                                description = detail.get("message", "Request failed") if isinstance(detail, dict) else "Request failed"
                                description = self.display_text(description)
                                future.set_exception(LspError(str(description)[:2000]))
                            else:
                                future.set_result(message.get("result"))
        except (OSError, ValueError, LspError) as error:
            self._fail(str(error))

    def _server_request(self, message):
        method, params = message["method"], message.get("params") or {}
        if method == "workspace/configuration":
            items = params.get("items", []) if isinstance(params, dict) else []
            if not isinstance(items, list):
                items = []
            result = []
            for item in items[:100]:
                value = self.settings
                section = item.get("section") if isinstance(item, dict) else None
                for part in section.split(".") if isinstance(section, str) and section else []:
                    value = value.get(part) if isinstance(value, dict) else None
                result.append(value)
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.root_uri, "name": "Naui workspace"}]
        elif method in {"client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create"}:
            result = None
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "Naui requires user-reviewed buffer edits"}
        else:
            self._send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported server request"}})
            return
        self._send({"id": message["id"], "result": result})

    def _writer(self):
        try:
            while not self._closed:
                try:
                    packet = self._output.get(timeout=0.1)
                except queue.Empty:
                    continue
                with self._lock:
                    self._queued_bytes -= len(packet)
                view = memoryview(packet)
                while view:
                    written = self.process.stdin.write(view)
                    if not written:
                        raise LspError("Language-server pipe closed")
                    view = view[written:]
        except (OSError, ValueError, LspError) as error:
            self._fail(str(error))

    def _stderr(self):
        # Drain without retaining unbounded logs or writing server output into
        # the terminal. Server stderr can contain source paths/content.
        try:
            while self.process.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _fail(self, message):
        with self._lock:
            if self._failed or self._closed:
                return
            self._failed = message
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(LspError(message))
            self._enqueue({"method": "naui/error", "params": {"message": message}})

    def _enqueue(self, message):
        # Translate user-visible text only; URIs must keep their physical form
        # until the originating device's mapper validates them.
        params = message.get('params')
        if isinstance(params, dict):
            params = dict(params)
            if isinstance(params.get('message'), str):
                params['message'] = self.display_text(params['message'])
            if isinstance(params.get('diagnostics'), list):
                params['diagnostics'] = [
                    {**item, 'message': self.display_text(item['message'])}
                    if isinstance(item, dict) and isinstance(item.get('message'), str) else item
                    for item in params['diagnostics']]
            message = {**message, 'params': params}
        size = len(json.dumps(message, ensure_ascii=True).encode("ascii"))
        if size > MAX_NOTIFICATIONS:
            message = {"method": "naui/error", "params": {"message": "Language-server notification exceeds 4 MiB; some diagnostics unavailable"}}
            size = 256
        with self._lock:
            if self._closed:
                return
            while self._messages and (self._message_bytes + size > MAX_NOTIFICATIONS or len(self._messages) >= 256):
                old_size, _ = self._messages.popleft()
                self._message_bytes -= old_size
            self._messages.append((size, message))
            self._message_bytes += size

    def drain(self):
        with self._lock:
            messages = [message for _, message in self._messages]
            self._messages.clear()
            self._message_bytes = 0
            return messages

    @property
    def failure(self):
        with self._lock:
            return self._failed

    def close(self):
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                process = self.process
            if process and process.poll() is None and not self._failed:
                try:
                    self.request("shutdown", None, timeout=0.5)
                    self.notify("exit", None)
                    process.wait(timeout=0.3)
                except (LspError, OSError, subprocess.TimeoutExpired):
                    pass
            with self._lock:
                self._closed = True
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(LspError("Language server stopped"))
                self._messages.clear()
                self._message_bytes = 0
            if process:
                # Each local server gets its own process group. Reap before
                # releasing pipes/cache, including misbehaving helper children.
                for signum in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        pass
                    if signum == signal.SIGTERM:
                        try:
                            process.wait(timeout=0.3)
                        except subprocess.TimeoutExpired:
                            pass
                process.wait()
                for thread in self._threads:
                    if thread is not threading.current_thread():
                        thread.join(timeout=1)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

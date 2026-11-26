"""Bounded streaming OSC parser. Ordinary terminal output is never interpreted.

Frames: ESC ] 777 ; naui ; BASE64(JSON UTF-8) BEL (or ESC backslash).
Each terminal has a random capability token. This is input validation, not a
security boundary against the user's own shell or a malicious remote host.
"""
import base64
import binascii
import json
import hashlib
import hmac
import secrets

PREFIX = b"\x1b]777;naui;"
MAX_PAYLOAD = 16384
MAX_PATH = 4096


class ProtocolError(ValueError):
    pass


def new_token():
    return secrets.token_hex(32)


def _string(value, maximum=MAX_PATH):
    return isinstance(value, str) and len(value.encode("utf-8")) <= maximum and "\0" not in value


def validate(message, token):
    if not isinstance(message, dict) or message.get("v") != 1:
        raise ProtocolError("Unsupported protocol version")
    supplied = message.get("token")
    if not isinstance(supplied, str) or not secrets.compare_digest(supplied, token):
        raise ProtocolError("Invalid terminal token")
    action = message.get("action")
    if action not in {"open", "cwd", "navigation-ready", "busy", "session-start", "session-ready", "session-end", "unsupported"}:
        raise ProtocolError("Unknown action")
    if action == "session-ready" and message.get("location") != "remote":
        raise ProtocolError("SSH readiness must originate from a remote session")
    if action in {"cwd", "navigation-ready", "busy"}:
        if message.get("location") not in {"local", "remote"}:
            raise ProtocolError("Invalid shell location")
        if action in {"cwd", "navigation-ready"}:
            for field in ("path", "home"):
                value = message.get(field)
                if not _string(value) or not value.startswith("/"):
                    raise ProtocolError("Invalid shell " + field)
    if action == "open":
        if message.get("location") not in {"local", "remote"}:
            raise ProtocolError("Invalid location")
        cwd, args = message.get("cwd"), message.get("args")
        if not _string(cwd) or not cwd.startswith("/"):
            raise ProtocolError("Invalid working directory")
        if not isinstance(args, list) or not 1 <= len(args) <= 32:
            raise ProtocolError("Expected 1 to 32 filenames")
        if any(not _string(arg) or not arg for arg in args):
            raise ProtocolError("Invalid filename")
        if not isinstance(message.get("elevated", False), bool):
            raise ProtocolError("Invalid elevation flag")
    if action in {"session-start", "session-ready", "session-end"} or message.get("location") == "remote":
        if not _string(message.get("session"), 64) or not message["session"]:
            raise ProtocolError("Invalid SSH session")
    if action == "session-start":
        if not _string(message.get("host"), 256) or not message["host"] or message["host"].startswith("-"):
            raise ProtocolError("Invalid SSH host")
        options = message.get("options")
        if not isinstance(options, list) or len(options) > 64 or any(not _string(x) for x in options):
            raise ProtocolError("Invalid SSH options")
        # Import here to keep protocol encoding usable without GTK or other dependencies.
        from .ssh import validate_transport_options
        validate_transport_options(options)
    if action == "unsupported" and not _string(message.get("reason"), 1024):
        raise ProtocolError("Invalid explanation")
    return message


def encode(message):
    payload = base64.b64encode(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode())
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("OSC payload too large")
    return PREFIX + payload + b"\x07"


def sign(message, key):
    unsigned = {k: v for k, v in message.items() if k != "signature"}
    data = json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return {**unsigned, "signature": hmac.new(key.encode(), data, hashlib.sha256).hexdigest()}


def verify_signature(message, key):
    supplied = message.get("signature")
    return isinstance(supplied, str) and hmac.compare_digest(supplied, sign(message, key)["signature"])


def decode(payload, token):
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("OSC payload too large")
    try:
        message = json.loads(base64.b64decode(payload, validate=True).decode("utf-8"))
        return validate(message, token)
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise ProtocolError(str(exc)) from exc


class OscParser:
    """Extract only our exact prefix, even when reads split an escape sequence.

    Other OSCs and arbitrary bytes pass through untouched. Overlong private
    frames are dropped with bounded memory until their terminator arrives.
    """
    def __init__(self, token, on_message, on_error=lambda error: None):
        self.token, self.on_message, self.on_error = token, on_message, on_error
        self.pending = bytearray()
        self.payload = None
        self.dropping = False
        self.escape = False

    def feed(self, data):
        output = bytearray()
        for byte in data:
            if self.payload is not None:
                if byte == 7 or (self.escape and byte == 92):
                    if byte == 92 and self.escape and not self.dropping:
                        del self.payload[-1:]
                    if not self.dropping:
                        try:
                            message = decode(bytes(self.payload), self.token)
                        except ProtocolError as exc:
                            self.on_error(str(exc))
                        else:
                            self.on_message(message)
                    self.payload, self.dropping, self.escape = None, False, False
                else:
                    if not self.dropping:
                        self.payload.append(byte)
                        if len(self.payload) > MAX_PAYLOAD:
                            self.payload.clear()
                            self.dropping = True
                            self.on_error("OSC payload too large")
                    self.escape = byte == 27
                continue
            self.pending.append(byte)
            while self.pending and not PREFIX.startswith(self.pending):
                output.append(self.pending.pop(0))
            if self.pending == PREFIX:
                self.pending.clear()
                self.payload = bytearray()
        return bytes(output)

    def finish(self):
        tail = bytes(self.pending)
        self.pending.clear()
        self.payload = None
        return tail

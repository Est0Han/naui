import base64
import json
import unittest

from naui.protocol import (MAX_PAYLOAD, PREFIX, OscParser, ProtocolError, decode,
                           encode, new_token, sign, verify_signature)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.token = new_token()
        self.message = {"v": 1, "token": self.token, "action": "open", "location": "local",
                        "cwd": "/tmp/space name", "args": ["é\n'\\\".py"], "elevated": False}

    def test_roundtrip(self):
        frame = encode(self.message)
        self.assertEqual(decode(frame[len(PREFIX):-1], self.token), self.message)

    def test_every_split_and_other_escapes(self):
        plain = b"hello\x1b[31m\x1b]0;Title\x07there\r\n"
        frame = encode(self.message)
        for index in range(len(frame) + 1):
            messages = []
            parser = OscParser(self.token, messages.append)
            output = parser.feed(plain + frame[:index]) + parser.feed(frame[index:] + plain)
            self.assertEqual(output, plain * 2)
            self.assertEqual(messages, [self.message])

    def test_single_byte_stream_and_st(self):
        messages = []
        parser = OscParser(self.token, messages.append)
        frame = encode(self.message)[:-1] + b"\x1b\\"
        self.assertEqual(b"".join(parser.feed(bytes([b])) for b in frame), b"")
        self.assertEqual(messages, [self.message])

    def test_invalid_frames_rejected(self):
        for payload in [b"!bad", base64.b64encode(b"[]"), base64.b64encode(b"{}"),
                        base64.b64encode(json.dumps({**self.message, "token": "bad"}).encode()),
                        base64.b64encode(json.dumps({**self.message, "args": ["bad\0path"]}).encode()),
                        base64.b64encode(json.dumps({**self.message, "cwd": "relative"}).encode()),
                        b"x" * (MAX_PAYLOAD + 1)]:
            with self.assertRaises(ProtocolError):
                decode(payload, self.token)

    def test_oversized_stream_is_bounded_and_recovers(self):
        messages, errors = [], []
        parser = OscParser(self.token, messages.append, errors.append)
        parser.feed(PREFIX + b"a" * (MAX_PAYLOAD * 8))
        self.assertLessEqual(len(parser.payload), MAX_PAYLOAD)
        self.assertEqual(parser.feed(b"\x07ok" + encode(self.message)), b"ok")
        self.assertEqual(messages, [self.message])
        self.assertTrue(errors)

    def test_signatures_cannot_be_reused_for_other_messages(self):
        key = new_token()
        message = sign(self.message, key)
        self.assertTrue(verify_signature(message, key))
        self.assertFalse(verify_signature({**message, "args": ["/etc/passwd"]}, key))
        self.assertFalse(verify_signature(message, new_token()))

    def test_unrecognized_osc_is_unchanged(self):
        data = b"\x1b]777;other;foo\x07\x1b]52;c;abc\x07\x00\xff"
        parser = OscParser(self.token, lambda _: self.fail("Unexpected event"))
        self.assertEqual(parser.feed(data), data)

    def test_cwd_busy_and_readiness_schema(self):
        for action in ["cwd", "navigation-ready", "busy"]:
            local = {"v": 1, "token": self.token, "action": action, "location": "local"}
            if action != "busy":
                local.update(path="/tmp/project ' õ\n", home="/home/user")
            frame = encode(local)
            self.assertEqual(decode(frame[len(PREFIX):-1], self.token), local)
            remote = {**local, "location": "remote", "session": "test-ssh"}
            frame = encode(remote)
            self.assertEqual(decode(frame[len(PREFIX):-1], self.token), remote)
            bad = {**remote, "session": ""}
            with self.assertRaises(ProtocolError):
                decode(encode(bad)[len(PREFIX):-1], self.token)
        for path in ["relative", "/tmp/bad\0", 42]:
            bad = {"v": 1, "token": self.token, "action": "cwd", "location": "local",
                   "path": path, "home": "/home/user"}
            with self.assertRaises(ProtocolError):
                decode(encode(bad)[len(PREFIX):-1], self.token)


if __name__ == "__main__":
    unittest.main()

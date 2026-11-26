"""User-selected local chat files, copied into a private session directory."""
from pathlib import Path
import os
import stat
import tempfile

MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 30 * 1024 * 1024


class Attachments:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='naui-chat-attachments-')
        self.total = 0
        self.sequence = 0

    def add_bytes(self, name, data):
        if not data or len(data) > MAX_FILE: raise ValueError('Choose a nonempty file up to 10 MB.')
        if self.total + len(data) > MAX_TOTAL: raise ValueError('This chat has reached its 30 MB attachment limit. Start a new panel session to attach more.')
        self.sequence += 1
        name = Path(name).name.replace('\0','')[:120] or 'attachment'
        path = Path(self.directory.name)/(str(self.sequence)+'-'+name)
        path.write_bytes(data); path.chmod(0o600)
        self.total += len(data)
        return {'name':name, 'path':str(path), 'size':len(data)}

    def add_file(self, path):
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise ValueError('Choose a regular file up to 10 MB.')
            data = stream.read(MAX_FILE+1)
        return self.add_bytes(Path(path).name, data)

    def close(self): self.directory.cleanup()

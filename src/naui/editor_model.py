"""Device-aware document metadata and text policy, independent of GTK.

A view supplies a buffer adapter with text()/dirty. Filesystem ownership never
changes when focus moves to another device, and save policy is explicitly opt-in.
"""
from dataclasses import dataclass
from typing import Protocol

from .external_changes import ExternalChange

LARGE_CHARACTERS = 1024 * 1024
LARGE_LINES = 20000
LARGE_LINE_LENGTH = 100000


class TextBuffer(Protocol):
    def text(self) -> str: ...
    @property
    def dirty(self) -> bool: ...


def large_file(text):
    return (len(text) >= LARGE_CHARACTERS or text.count('\n') >= LARGE_LINES
            or any(len(line) >= LARGE_LINE_LENGTH for line in text.split('\n')))


def save_text(text, *, trim=False, final_newline=False):
    """Keep line endings and final-newline state unless explicitly configured."""
    if trim:
        text = '\n'.join(line.rstrip(' \t') if not line.endswith('\r')
                         else line[:-1].rstrip(' \t') + '\r' for line in text.split('\n'))
    if final_newline and text and not text.endswith('\n'):
        text += '\r\n' if '\r\n' in text else '\n'
    return text


def shebang_language(text):
    """Interpreter hint only: never execute a shebang or opened script."""
    import re
    first = text.split('\n', 1)[0][:512]
    if not first.startswith('#!'):
        return None
    words = re.findall(r'[A-Za-z0-9_.+-]+', first[2:])
    for word in reversed(words):
        if re.fullmatch(r'python(?:[23](?:\.\d+)*)?', word): return 'python3'
        if word in {'bash', 'sh', 'zsh', 'dash', 'ksh'}: return 'sh'
        if word in {'node', 'nodejs'}: return 'js'
    return None


@dataclass
class EditorDocument:
    device_id: str | None
    path: str
    backend: object
    buffer: TextBuffer | None = None
    revision: str | None = None
    encoding: str = 'utf-8'
    language: str | None = None
    large: bool = False
    lsp_version: int = 0

    def __post_init__(self):
        self.external = ExternalChange()

    @property
    def dirty(self): return bool(self.buffer and self.buffer.dirty)

    def text(self): return self.buffer.text() if self.buffer else ''

    def loaded(self, snapshot):
        self.path, self.revision = snapshot.path, snapshot.revision
        self.large = large_file(snapshot.content)
        self.external.clear()

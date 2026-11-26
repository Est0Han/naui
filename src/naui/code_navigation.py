"""Device-owned locations, UTF-16 LSP positions and read-only path links."""
from dataclasses import dataclass
from bisect import bisect_right
import os
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit

from .navigation import navigation_path


@dataclass(frozen=True)
class CodeLocation:
    device_id: str
    path: str
    line: int = 0
    character: int = 0  # LSP UTF-16 code units, not Python/GTK character offsets.


def file_uri(path):
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError("Expected an absolute file path")
    return Path(path).as_uri()


def uri_path(uri):
    if not isinstance(uri, str) or len(uri) > 32768:
        raise ValueError("Invalid file URI")
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
        raise ValueError("Only local file URIs in the originating device context are supported")
    path = unquote(parsed.path, errors="strict")
    if not path.startswith("/") or "\0" in path:
        raise ValueError("Invalid file URI path")
    return posixpath.normpath(path)


def within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


class UriMapper:
    """An LSP URI never supplies a device identity; its session supplies it.

    Remote URIs must be inside THIS device's private mirror. Local stdlib/stub
    definitions may be outside a project; rename remains restricted to its root.
    """
    def __init__(self, device_id, root, mirror=None):
        self.device_id, self.root = device_id, posixpath.normpath(root)
        self.mirror = os.path.realpath(mirror) if mirror else None

    def analysis_path(self, path):
        path = posixpath.normpath(path)
        if not path.startswith("/") or "\0" in path:
            raise ValueError("Expected an absolute document path")
        if self.mirror:
            if not within(path, self.root):
                raise ValueError("Remote location is outside the mirrored project")
            relative = posixpath.relpath(path, self.root)
            result = os.path.realpath(os.path.join(self.mirror, relative))
            if not within(result, self.mirror):
                raise ValueError("Mirror path escaped its device")
            return result
        return path

    def uri(self, path):
        return file_uri(self.analysis_path(path))

    def public_text(self, text):
        if not self.mirror:
            return str(text)
        return str(text).replace(file_uri(self.mirror), self.root).replace(self.mirror, self.root)

    def location(self, uri, position=None, *, mutation=False):
        path = uri_path(uri)
        if self.mirror:
            physical = os.path.realpath(path)
            if not within(physical, self.mirror):
                raise ValueError("Definition is outside this remote source mirror (remote libraries are not mirrored)")
            path = posixpath.normpath(posixpath.join(self.root, os.path.relpath(physical, self.mirror)))
        if mutation and not within(os.path.realpath(path) if not self.mirror else path, self.root):
            raise ValueError("Rename edits outside the project are rejected")
        line, character = validate_position(position or {"line": 0, "character": 0})
        return CodeLocation(self.device_id, path, line, character)


def validate_position(position):
    if not isinstance(position, dict):
        raise ValueError("Invalid LSP position")
    line, character = position.get("line"), position.get("character")
    if any(type(value) is not int or value < 0 or value > 8_388_608 for value in (line, character)):
        raise ValueError("Invalid LSP position")
    return line, character


class TextPositions:
    """Index once for batches of edits/diagnostics, with LSP line terminators."""
    def __init__(self, text):
        self.text = text
        self.starts = [0, *[match.end() for match in re.finditer(r"\r\n|\r|\n", text)]]

    def offset(self, position, *, clamp=False):
        line, character = validate_position(position)
        if line >= len(self.starts):
            if clamp:
                return len(self.text)
            raise ValueError("LSP line is outside the document")
        start = self.starts[line]
        end = self.starts[line + 1] if line + 1 < len(self.starts) else len(self.text)
        content = self.text[start:end].rstrip("\r\n")
        units = 0
        for index, char in enumerate(content):
            if units == character:
                return start + index
            units += 2 if ord(char) > 0xFFFF else 1
            if units > character:
                if clamp:
                    return start + index
                raise ValueError("LSP position splits a UTF-16 surrogate pair")
        if units != character and not clamp:
            raise ValueError("LSP character is outside the line")
        return start + len(content)

    def position(self, offset):
        if type(offset) is not int or not 0 <= offset <= len(self.text):
            raise ValueError("Invalid text offset")
        line = bisect_right(self.starts, offset) - 1
        tail = self.text[self.starts[line]:offset]
        return {"line": line, "character": len(tail.encode("utf-16-le")) // 2}


def offset_at(text, position, *, clamp=False):
    return TextPositions(text).offset(position, clamp=clamp)


def position_at(text, offset):
    return TextPositions(text).position(offset)


def apply_text_edits(text, edits):
    if not isinstance(edits, list) or len(edits) > 20000:
        raise ValueError("Invalid or excessive rename edits")
    changes = []
    positions = TextPositions(text)
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("newText"), str):
            raise ValueError("Invalid rename edit")
        span = edit.get("range", {})
        if not isinstance(span, dict):
            raise ValueError("Invalid rename range")
        first, last = positions.offset(span.get("start")), positions.offset(span.get("end"))
        if last < first:
            raise ValueError("Reversed rename range")
        changes.append((first, last, edit["newText"]))
    changes.sort(key=lambda change: (change[0], change[1]))
    previous = -1
    for first, last, _ in changes:
        if first < previous:
            raise ValueError("Overlapping rename edits")
        previous = last
    for first, last, replacement in reversed(changes):
        text = text[:first] + replacement + text[last:]
    if len(text.encode()) > 8 * 1024 * 1024 or "\0" in text:
        raise ValueError("Rename would exceed the text editor's limits")
    return text


def edit_groups(edit, mapper):
    """Reject resource operations, foreign URIs and ambiguous edit encodings."""
    if not isinstance(edit, dict) or ("changes" in edit and "documentChanges" in edit):
        raise ValueError("Invalid workspace edit")
    groups = []
    if "changes" in edit:
        if not isinstance(edit["changes"], dict):
            raise ValueError("Invalid workspace changes")
        groups = [(uri, None, edits) for uri, edits in edit["changes"].items()]
    elif "documentChanges" in edit:
        if not isinstance(edit["documentChanges"], list):
            raise ValueError("Invalid document changes")
        for item in edit["documentChanges"]:
            if not isinstance(item, dict) or "kind" in item or not isinstance(item.get("textDocument"), dict):
                raise ValueError("File create/rename/delete requests from language servers are not supported")
            document = item["textDocument"]
            groups.append((document.get("uri"), document.get("version"), item.get("edits")))
    else:
        raise ValueError("Language server returned no rename edits")
    if not 0 < len(groups) <= 64:
        raise ValueError("Rename must affect between 1 and 64 files")
    result, seen = [], set()
    for uri, version, edits in groups:
        location = mapper.location(uri, mutation=True)
        if location.path in seen or (version is not None and (type(version) is not int or version < 0)):
            raise ValueError("Duplicate or invalid rename document")
        if not isinstance(edits, list):
            raise ValueError("Invalid rename edits")
        seen.add(location.path)
        result.append((location, version, edits))
    return result


class CodeHistory:
    def __init__(self):
        self.entries, self.index = [], -1

    def add(self, location):
        if self.index >= 0 and self.entries[self.index] == location:
            return
        del self.entries[self.index + 1:]
        self.entries.append(location)
        if len(self.entries) > 200:
            self.entries.pop(0)
        self.index = len(self.entries) - 1

    def target(self, direction):
        index = self.index + direction
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def accept(self, direction):
        if self.target(direction):
            self.index += direction


# Compatibility exports; the bundled filepath plugin owns parsing/highlighting.
from .plugins.base_filepath import PathLink, PATH_PATTERN, PATH_RE, TERMINAL_PATH_PATTERN, path_links

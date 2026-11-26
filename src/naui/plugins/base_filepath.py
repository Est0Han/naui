"""Bundled read-only path navigation, resolved through the public device API."""
from dataclasses import dataclass
import os
import re

from ..navigation import navigation_path


@dataclass(frozen=True)
class PathLink:
    start: int
    end: int
    location: "CodeLocation"


# Quoted paths cover spaces and tracebacks. Bare links include conventional
# path/filename:line:column forms. URL contents are never shell commands.
PATH_PATTERN = r'''(?:file://[^\s<>"']{1,16384}|["'][^"'\n]{1,4096}["'](?:,\s*line\s+\d{1,9}|:\d{1,9}(?::\d{1,9})?)?|(?:~?/|\./|\.\./)[^\s<>"'()\[\]{}]{1,4096}|(?<![\w@.+/-])[\w@.+-]{1,255}(?:/[\w@.+-]{1,255}){0,16}\.[A-Za-z0-9_]{1,16}(?::\d{1,9}(?::\d{1,9})?)?)'''
# Compiler output also uses path(line,column), notably C# and MSBuild.
PATH_PATTERN = '(?:' + PATH_PATTERN + r')(?:\(\d{1,9}(?:,\d{1,9})?\))?'
PATH_RE = re.compile(PATH_PATTERN)
# VTE must match URLs as a whole. Matching just a domain/path substring would
# lose its URL context before path_links can reject non-filesystem schemes.
TERMINAL_PATH_PATTERN = r'''(?:(?<![\w+.-])[A-Za-z][A-Za-z0-9+.-]{0,63}://[^\s<>"']{1,16384}|''' + PATH_PATTERN + ")"


def path_links(text, cwd, device_id, home=None, *, remote=False):
    from ..code_navigation import CodeLocation, uri_path
    results = []
    urls = [(match.start(), match.end()) for match in re.finditer(r"(?<![\w+.-])[A-Za-z][A-Za-z0-9+.-]{0,63}://[^\s<>\"']{1,16384}", text[:1024 * 1024])
            if not match.group().startswith("file://")]
    for match in PATH_RE.finditer(text[:1024 * 1024]):
        if any(start <= match.start() < end for start, end in urls):
            continue
        candidate = match.group().rstrip(".,;")
        if match.start() and text[max(0, match.start() - 8):match.start()].endswith(("http:", "https:", ":/")):
            continue
        line = column = 0
        if candidate.startswith(("'", '"')):
            quote = candidate[0]
            end = candidate.find(quote, 1)
            path, suffix = candidate[1:end], candidate[end + 1:]
            if not ("/" in path or re.search(r"\.[\w]+$", path)):
                continue
            numbered = re.search(r"(?:line\s+|:)(\d+)(?::(\d+))?", suffix)
        else:
            numbered = re.search(r"(?:[:(])(\d+)(?:[:,](\d+))?\)?$", candidate)
            path = candidate[:numbered.start()] if numbered else candidate
        if numbered:
            line, column = max(0, int(numbered[1]) - 1), max(0, int(numbered[2] or 1) - 1)
        if line > 8_388_608 or column > 8_388_608:
            continue
        try:
            if "://" in path and not path.startswith("file://"):
                continue
            if path.startswith("file://"):
                path = uri_path(path)
            else:
                path = navigation_path(path, cwd, home)
            if not remote:
                path = os.path.realpath(path)
            results.append(PathLink(match.start(), match.start() + len(candidate), CodeLocation(device_id, path, line, column)))
        except (ValueError, UnicodeError):
            continue
        if len(results) >= 1000:
            break
    return results


def start(api):
    from ..transfers import FileLocation
    api.register_feature('path_parser', path_links)
    api.register_feature('terminal_path_pattern', TERMINAL_PATH_PATTERN)
    def open_path(location):
        api.open(FileLocation(location.device_id, location.path), location.line, location.character)
    api.register_feature('path_open', open_path)

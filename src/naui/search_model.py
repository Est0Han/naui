"""Portable workspace search queries and results; no widgets or mount paths."""
from dataclasses import dataclass, field
import base64
import fnmatch
import json
import posixpath
import re

DEFAULT_EXCLUDES = tuple('**/' + name + '/**' for name in
                         ('.git', 'node_modules', '__pycache__', '.venv', 'venv', 'build', 'dist', '.cache'))


def patterns(text):
    values = tuple(value.strip() for value in text.split(',') if value.strip())
    if len(values) > 64 or any(len(value) > 512 or '\0' in value or value.startswith('!') or '{' in value or '}' in value for value in values):
        raise ValueError('Use at most 64 comma-separated positive glob patterns (512 characters each).')
    return values


@dataclass(frozen=True)
class SearchRoot:
    device_id: str
    path: str
    backend: object = field(compare=False, repr=False)
    remote: bool = False
    label: str = ''

    def __post_init__(self):
        if not self.device_id or not self.path.startswith('/') or '\0' in self.path or len(self.path.encode()) > 4096:
            raise ValueError('Search roots need a device and an absolute logical path.')
        object.__setattr__(self, 'path', posixpath.normpath(self.path))


@dataclass(frozen=True)
class SearchQuery:
    text: str
    mode: str = 'content'
    case_sensitive: bool = False
    whole_word: bool = False
    regex: bool = False
    include_patterns: tuple = ()
    exclude_patterns: tuple = DEFAULT_EXCLUDES
    include_hidden: bool = False
    include_ignored: bool = False
    max_file_size: int = 10 * 1024 * 1024
    max_results: int = 10000
    max_entries: int = 100000
    roots: tuple = ()

    def __post_init__(self):
        if self.mode not in {'files', 'content'} or not isinstance(self.text, str) or not self.text or any(char in self.text for char in ('\0','\n','\r')) or len(self.text.encode()) > 4096:
            raise ValueError('Enter a search query (up to 4096 UTF-8 bytes).')
        if self.regex and self.mode == 'content':
            try: re.compile(self.text)
            except (re.error, RecursionError, OverflowError) as error:
                raise ValueError('Invalid regex: ' + str(error)) from error
        if any(type(value) is not bool for value in (self.case_sensitive,self.whole_word,self.regex,self.include_hidden,self.include_ignored)):
            raise ValueError('Search options must be booleans.')
        if not isinstance(self.roots,tuple) or len(self.roots) > 512 or any(not isinstance(root,SearchRoot) for root in self.roots):
            raise ValueError('Invalid workspace roots.')
        for value in (self.include_patterns, self.exclude_patterns):
            if not isinstance(value, tuple) or len(value) > 64: raise ValueError('Invalid search filters.')
            patterns(','.join(value))
        if any(type(value) is not int for value in (self.max_results,self.max_file_size,self.max_entries)) or not 1 <= self.max_results <= 10000 or not 1 <= self.max_file_size <= 100 * 1024 * 1024 or not 1 <= self.max_entries <= 100000:
            raise ValueError('Search limits are outside the supported range.')


@dataclass(frozen=True)
class SearchResult:
    device_id: str
    workspace_root: str
    logical_path: str
    line: int = 0  # 1-based; 0 means a filename result
    column: int = 0  # 0-based Unicode characters, not bytes or UTF-16
    match_start: int = 0
    match_end: int = 0  # offsets in the original logical line
    preview: str = ''
    score: int = 0
    directory: bool = False

    @property
    def identity(self): return self.device_id, self.logical_path
    @property
    def relative_path(self): return posixpath.relpath(self.logical_path, self.workspace_root)


def glob_matches(path, pattern):
    """Shared component globs: *, ?, [], and zero-or-more-component **."""
    if path.startswith('./'): path = path[2:]
    pattern = pattern.lstrip('/')
    if '/' not in pattern: return fnmatch.fnmatchcase(posixpath.basename(path), pattern)
    names, parts = path.split('/'), pattern.split('/')
    pending, seen = [(0,0)], set()
    while pending:
        name, part = pending.pop()
        if (name,part) in seen: continue
        seen.add((name,part))
        if part == len(parts):
            if name == len(names): return True
        elif parts[part] == '**':
            pending.append((name,part+1))
            if name < len(names): pending.append((name+1,part))
        elif name < len(names) and fnmatch.fnmatchcase(names[name],parts[part]):
            pending.append((name+1,part+1))
    return False


def allowed(path, query, directory=False):
    if not query.include_hidden and any(part.startswith('.') for part in path.split('/')): return False
    if any(glob_matches(path, pattern) or (directory and glob_matches(path + '/', pattern)) for pattern in query.exclude_patterns): return False
    return directory or not query.include_patterns or any(glob_matches(path, pattern) for pattern in query.include_patterns)


def file_score(query, path, case_sensitive=False):
    name = posixpath.basename(path)
    if not case_sensitive: query, name, path = query.casefold(), name.casefold(), path.casefold()
    depth = path.count('/')
    if query == name: return 10000 - depth
    if name.startswith(query): return 9000 - len(name) - depth
    if query in name: return 8000 - name.index(query) - depth
    def fuzzy(value):
        cursor, gap = 0, 0
        for char in query:
            found = value.find(char, cursor)
            if found < 0: return None
            gap += found - cursor; cursor = found + 1
        return gap
    gap = fuzzy(name)
    if gap is not None: return 6000 - gap - len(name) - depth
    if query in path: return 4000 - path.index(query) - depth
    gap = fuzzy(path)
    return 2000 - gap - len(path) - depth if gap is not None else None


def preview(text, start, end, width=240):
    text = text.rstrip('\r\n')
    offset = max(0, start - width // 3)
    return ('…' if offset else '') + text[offset:offset + width] + ('…' if len(text) > offset + width else '')


def decode_rg(value):
    if 'text' in value: return value['text']
    return base64.b64decode(value['bytes'], validate=True).decode('utf-8')


def parse_rg(record, root):
    value = json.loads(record)
    if value.get('type') != 'match': return []
    data = value['data']; relative = decode_rg(data['path'])
    if not isinstance(relative,str) or '\0' in relative or len(relative.encode()) > 4096:
        raise ValueError('Invalid search result path.')
    if type(data['line_number']) is not int or not 1 <= data['line_number'] <= 8388608:
        raise ValueError('Invalid search line number.')
    path = posixpath.normpath(posixpath.join(root.path, relative))
    if not path.startswith(root.path.rstrip('/') + '/') or path == root.path:
        raise ValueError('Search response escaped its workspace root.')
    text = decode_rg(data['lines']); encoded = text.encode('utf-8')
    results = []
    for match in data['submatches']:
        first, last = match['start'], match['end']
        if not 0 <= first <= last <= len(encoded): raise ValueError('Invalid search match offsets.')
        start, end = len(encoded[:first].decode('utf-8')), len(encoded[:last].decode('utf-8'))
        results.append(SearchResult(root.device_id, root.path, path, data['line_number'], start, start, end, preview(text, start, end)))
    return results


def rg_arguments(query):
    argv = ['rg', '--no-config', '--no-follow', '--json', '--color=never', '--max-filesize', str(query.max_file_size)]
    if not query.case_sensitive: argv.append('--ignore-case')
    if query.whole_word: argv.append('--word-regexp')
    if not query.regex: argv.append('--fixed-strings')
    if query.include_hidden: argv.append('--hidden')
    if query.include_ignored: argv.append('--no-ignore')
    for glob in query.include_patterns: argv.extend(['--glob', glob])
    for glob in query.exclude_patterns: argv.extend(['--glob', '!' + glob])
    argv.extend(['--', query.text, '.'])
    return tuple(argv)

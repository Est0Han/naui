"""Bounded unified patch model, independent of GTK, Git and device transports."""
from dataclasses import dataclass, field
import re

AUTO_BYTES = 1024 * 1024
AUTO_LINES = 12000
HARD_BYTES = 8 * 1024 * 1024
HARD_LINES = 60000
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$')


@dataclass(frozen=True)
class DiffLine:
    kind: str
    text: str
    old_number: int | None
    new_number: int | None


@dataclass
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    heading: str
    lines: list = field(default_factory=list)


@dataclass
class DiffDocument:
    old_path: str = ''
    new_path: str = ''
    hunks: list = field(default_factory=list)
    binary: bool = False
    metadata: list = field(default_factory=list)


@dataclass
class DiffModel:
    documents: list

    @property
    def hunks(self): return [h for doc in self.documents for h in doc.hunks]


def size_mode(patch):
    if not isinstance(patch, str) or '\0' in patch: raise ValueError('Expected a text patch without NUL bytes')
    size, lines = len(patch.encode('utf-8')), patch.count('\n')
    if size > HARD_BYTES or lines > HARD_LINES: return 'blocked'
    return 'large' if size > AUTO_BYTES or lines > AUTO_LINES else 'normal'


def parse_diff(patch):
    if size_mode(patch) == 'blocked': raise ValueError('Diff exceeds rendering limit')
    documents, doc, hunk = [], None, None
    old, new = 0, 0
    def finish():
        if hunk and (sum(line.old_number is not None for line in hunk.lines) != hunk.old_count
                     or sum(line.new_number is not None for line in hunk.lines) != hunk.new_count):
            raise ValueError('Truncated or inconsistent unified diff hunk')
    for raw in patch.splitlines():
        # A file's content may itself start with ---/+++; consume exact hunk
        # counts before interpreting another file header.
        remaining = hunk and (old < hunk.old_start + hunk.old_count or new < hunk.new_start + hunk.new_count)
        if remaining:
            if raw.startswith('\\ No newline at end of file'):
                hunk.lines.append(DiffLine('note', raw, None, None)); continue
            if not raw or raw[0] not in ' +-': raise ValueError('Invalid unified diff line')
            kind = {' ': 'context', '+': 'added', '-': 'removed'}[raw[0]]
            left, right = old if raw[0] != '+' else None, new if raw[0] != '-' else None
            hunk.lines.append(DiffLine(kind, raw[1:], left, right))
            old += left is not None; new += right is not None
            continue
        if raw.startswith('\\ No newline at end of file') and hunk:
            hunk.lines.append(DiffLine('note', raw, None, None)); continue
        match = HUNK.match(raw)
        if match:
            finish()
            if doc is None: doc = DiffDocument(); documents.append(doc)
            a, b, c, d, heading = match.groups()
            hunk = DiffHunk(int(a), int(b) if b is not None else 1, int(c), int(d) if d is not None else 1, heading.strip())
            if hunk.old_count + hunk.new_count > HARD_LINES: raise ValueError('Diff hunk exceeds limit')
            old, new = hunk.old_start, hunk.new_start
            doc.hunks.append(hunk)
        elif raw.startswith('diff --git '):
            finish(); hunk = None
            doc = DiffDocument(metadata=[raw]); documents.append(doc)
        elif raw.startswith('--- '):
            finish(); hunk = None
            if doc is None or doc.hunks or doc.old_path:
                doc = DiffDocument(); documents.append(doc)
            doc.old_path = raw[4:].split('\t', 1)[0]
        elif raw.startswith('+++ '):
            if doc is None: raise ValueError('New diff path without old path')
            doc.new_path = raw[4:].split('\t', 1)[0]
        elif raw.startswith(('Binary files ', 'GIT binary patch')):
            if doc is None: doc = DiffDocument(); documents.append(doc)
            doc.binary = True; doc.metadata.append('Binary file changed')
        elif hunk and raw.startswith(('+', '-')):
            raise ValueError('Unexpected unified diff line beyond hunk counts')
        elif doc is not None and not hunk:
            # Retain rename/mode metadata. Never use these strings for commands.
            if raw.startswith(('rename ', 'copy ', 'old mode ', 'new mode ', 'new file mode ', 'deleted file mode ', 'similarity ')):
                doc.metadata.append(raw)
    finish()
    return DiffModel(documents)


def side_rows(hunk):
    """Align contiguous deletion/addition blocks with padding, without inventing lines."""
    rows, removed, added = [], [], []
    def flush():
        for i in range(max(len(removed), len(added))):
            rows.append((removed[i] if i < len(removed) else None, added[i] if i < len(added) else None))
        removed.clear(); added.clear()
    for line in hunk.lines:
        if line.kind == 'removed':
            if added: flush()
            removed.append(line)
        elif line.kind == 'added': added.append(line)
        elif line.kind == 'context': flush(); rows.append((line, line))
        else: flush(); rows.append((line, line))
    flush()
    return rows


def text_diff(old, new, old_path, new_path):
    """Unified text patch with explicit missing-newline markers (also for AI review)."""
    import difflib
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=old_path, tofile=new_path)
    result = []
    for line in lines:
        result.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    return ''.join(result)

"""Device-aware Git commands and bounded machine-readable status models.

All commands use the public command service: SSHFS never changes execution
context. Paths remain separate argv values with literal Git pathspec semantics.
"""
from dataclasses import dataclass
import posixpath

GIT = ('git', '--no-pager', '--literal-pathspecs', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-c', 'core.quotepath=false')


@dataclass(frozen=True)
class GitChange:
    path: str
    index: str
    worktree: str
    original: str | None = None
    conflicted: bool = False
    directory: bool = False

    @property
    def decoration(self):
        if self.conflicted: return '!'
        if self.index in {'?', '!'}: return self.index
        return next((code for code in 'RCDATM' if code in (self.index, self.worktree)), 'M')

    def code(self, staged=False):
        return '!' if self.conflicted else ('?' if self.index == '?' else self.index if staged else self.worktree)

    @property
    def paths(self):
        return (self.original, self.path) if self.original else (self.path,)


@dataclass(frozen=True)
class GitStatus:
    branch: str
    ahead: int
    behind: int
    changes: tuple
    head: str | None = None
    upstream: str | None = None


def git_path(value):
    text = value.decode('utf-8')
    if not text or text.startswith('/') or '\0' in text or any(part in {'', '..', '.'} for part in text.split('/')):
        # File paths are relative data, never executable shell/pathspec syntax.
        raise ValueError('Invalid Git relative path')
    return text


def parse_status(data):
    if len(data) > 8 * 1024 * 1024: raise ValueError('Git status exceeds limit')
    records, changes, branch, ahead, behind = data.split(b'\0'), [], '', 0, 0
    index, head, upstream = 0, None, None
    while index < len(records):
        record = records[index]; index += 1
        if not record: continue
        if record.startswith(b'# branch.oid '):
            value = record[13:].decode('ascii')
            if value != '(initial)':
                from .git_history import oid
                head = oid(value)
        elif record.startswith(b'# branch.head '): branch = record[14:].decode('utf-8', 'replace')
        elif record.startswith(b'# branch.upstream '): upstream = record[18:].decode('utf-8', 'replace')
        elif record.startswith(b'# branch.ab '):
            counts = record[12:].split()
            if len(counts) != 2: raise ValueError('Invalid Git branch counters')
            ahead, behind = abs(int(counts[0])), abs(int(counts[1]))
        elif record.startswith(b'! '): changes.append(GitChange(git_path(record[2:].removesuffix(b'/')), '!', '!', directory=record.endswith(b'/')))
        elif record.startswith(b'? '): changes.append(GitChange(git_path(record[2:].removesuffix(b'/')), '?', '?', directory=record.endswith(b'/')))
        elif record[:2] in {b'1 ', b'2 ', b'u '}:
            count = {b'1 ': 8, b'2 ': 9, b'u ': 10}[record[:2]]
            fields = record.split(b' ', count)
            if len(fields) != count + 1 or len(fields[1]) != 2: raise ValueError('Invalid Git status record')
            x, y = fields[1].decode('ascii')
            if any(char not in '.MADRCUT' for char in (x, y)): raise ValueError('Invalid Git status code')
            original = None
            if record.startswith(b'2 '):
                if index >= len(records) or not records[index]: raise ValueError('Missing renamed Git path')
                original = git_path(records[index]); index += 1
            changes.append(GitChange(git_path(fields[-1]), x, y, original, record.startswith(b'u ')))
        elif not record.startswith((b'# ', b'! ')):
            raise ValueError('Unknown Git status record')
        if len(changes) > 20000: raise ValueError('Git change-count limit reached')
    return GitStatus(branch or '(detached)', ahead, behind, tuple(changes), head, upstream)


def repository_path(data):
    path = data.decode('utf-8').removesuffix('\n')
    if not path.startswith('/') or '\0' in path or len(path.encode()) > 4096:
        raise ValueError('Invalid repository root')
    return posixpath.normpath(path)



class GitService:
    def __init__(self, api): self.api = api

    def execute(self, repo, command, callback):
        if not repo.connected or not repo.active:
            callback(None, RuntimeError('Repository unavailable: device disconnected or root removed'))
            return
        try:
            self.api.commands.execute(device_id=repo.device_id, cwd=repo.root,
                                      command=(*GIT, *command), callback=callback)
        except (OSError, ValueError, RuntimeError, KeyError) as error: callback(None, error)

    def status(self, repo, callback):
        self.execute(repo, ('status', '--porcelain=v2', '-z', '--branch',
                           '--untracked-files=all', '--ignore-submodules=all'), callback)

    def diff(self, repo, change, staged, callback):
        paths = tuple(git_path(path.encode()) for path in change.paths)
        self.execute(repo, ('diff', '--no-color', '--no-ext-diff', '--no-textconv',
                           '--ignore-submodules', *(('--cached',) if staged else ()), '--', *paths), callback)

    def operation(self, repo, action, callback, change=None, message=None):
        # Do not accept caller-supplied Git option strings as operations.
        paths = tuple(git_path(path.encode()) for path in change.paths) if change else ('.',)
        if action == 'stage':
            # An index rename's original path is already removed from the index.
            # Stage only this file, rather than another edit at its old path.
            stage_paths = paths if change and change.worktree == 'R' else (change.path,) if change else ('.',)
            command = ('add', '--', *stage_paths) if change else ('add', '-A', '--', '.')
        elif action == 'unstage':
            if repo.status and repo.status.head:
                command = ('reset', '-q', 'HEAD', '--', *paths)
            else:
                command = ('rm', '--cached', '-f', '-q', '--ignore-unmatch', *(('-r',) if change is None else ()), '--', *paths)
        elif action == 'discard':
            if change is None or change.conflicted or change.index == '!':
                raise ValueError('Discard requires one non-conflicted file')
            if change.index == '?':
                # Git clean with an explicit path can recursively delete a
                # directory even without -d. Use revision-checked file removal.
                from ..transfers import FileLocation
                location = FileLocation(repo.device_id, posixpath.join(repo.root, change.path))
                def properties(entry, error):
                    if error: callback(None, error)
                    elif entry.is_directory: callback(None, ValueError('Discard refuses directories'))
                    else:
                        try: self.api.files.remove(location, entry.revision, callback)
                        except (OSError, ValueError, RuntimeError, KeyError) as exc: callback(None, exc)
                self.api.properties(location, properties)
                return
            from ..transfers import FileLocation
            location = FileLocation(repo.device_id, posixpath.join(repo.root, change.path))
            command = ('restore', '--worktree', '--', change.path)
            def properties(entry, error):
                if error: callback(None, error)
                elif entry.is_directory: callback(None, ValueError('Discard refuses a file replaced by a directory'))
                else: self.execute(repo, command, callback)
            def exists(present, error):
                if error: callback(None, error)
                elif present: self.api.properties(location, properties)
                else: self.execute(repo, command, callback)
            # Git restore can recursively erase an untracked directory replacing
            # a tracked file. Check via the device backend before invoking it.
            self.api.exists(location, exists)
            return
        elif action == 'commit':
            if not message or not message.strip(): raise ValueError('Enter a commit message')
            if len(message.encode()) > 16000 or '\0' in message: raise ValueError('Invalid or oversized commit message')
            if not repo.status or not any(c.index not in {'.', '?', '!'} and not c.conflicted for c in repo.status.changes):
                raise ValueError('Stage changes before committing')
            if any(c.conflicted for c in repo.status.changes): raise ValueError('Resolve conflicts before committing')
            command = ('commit', '-m', message, '--')
        elif action == 'fetch': command = ('fetch', '--')
        elif action in {'push', 'pull'}:
            if not repo.status or not repo.status.upstream: raise ValueError('Configure an upstream before ' + action)
            command = ('pull', '--ff-only', '--') if action == 'pull' else ('push', '--')
        else: raise ValueError('Unknown Git operation')
        self.execute(repo, command, callback)

    def outgoing(self, repo, callback):
        from .git_history import LOG_FORMAT
        self.execute(repo, ('log', '-z', '--no-show-signature', '--decorate=short',
                           '--topo-order', '--max-count=21', '--format=' + LOG_FORMAT,
                           '@{upstream}..HEAD', '--'), callback)

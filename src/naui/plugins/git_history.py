"""Bounded Git history and commit details using only public device/view APIs."""
from dataclasses import dataclass
from datetime import datetime, timezone
import posixpath
import re

from .base_git import GIT, git_path

LOG_FORMAT = '%H%x00%P%x00%an%x00%at%x00%s%x00%D'
DETAIL_FORMAT = '%H%x00%P%x00%an%x00%ae%x00%at%x00%B'
REF_FORMAT = '%(refname)%00%(objectname)%00%(HEAD)'
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def oid(value):
    if not OID.fullmatch(value): raise ValueError('Invalid Git object ID')
    return value


def refname(value):
    if (not value.startswith(('refs/heads/', 'refs/remotes/', 'refs/tags/'))
            or len(value.encode()) > 4096 or any(ord(char) < 33 or char in '~^:?*[\\' for char in value)
            or '..' in value or '@{' in value or value.endswith(('/', '.', '.lock'))):
        raise ValueError('Invalid Git reference')
    return value


def records(data, columns, maximum):
    if len(data) > 8 * 1024 * 1024: raise ValueError('Git history exceeds output limit')
    if not data: return []
    if not data.endswith(b'\0'): raise ValueError('Truncated Git history record')
    fields = data[:-1].split(b'\0')
    if len(fields) % columns or len(fields) // columns > maximum: raise ValueError('Invalid Git history fields/count')
    return [fields[index:index + columns] for index in range(0, len(fields), columns)]


@dataclass(frozen=True)
class Commit:
    hash: str
    parents: tuple
    author: str
    timestamp: int
    summary: str
    refs: str

    @property
    def date(self): return datetime.fromtimestamp(self.timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def parse_log(data, maximum=101):
    result = []
    for hash, parents, author, timestamp, subject, refs in records(data, 6, maximum):
        time = int(timestamp)
        if not 0 <= time <= 32503680000: raise ValueError('Invalid commit timestamp')
        result.append(Commit(oid(hash.decode('ascii')), tuple(oid(item.decode('ascii')) for item in parents.split()),
                             author.decode('utf-8', 'replace'), time, subject.decode('utf-8', 'replace'), refs.decode('utf-8', 'replace')))
    if len({commit.hash for commit in result}) != len(result): raise ValueError('Duplicate commits')
    return result


def parse_refs(data):
    if len(data) > 8 * 1024 * 1024: raise ValueError('Git refs exceed output limit')
    result = []
    for record in data.splitlines():
        parts = record.split(b'\0')
        if len(parts) != 3 or parts[2] not in (b' ', b'*', b''): raise ValueError('Invalid Git ref record')
        name = refname(parts[0].decode('utf-8'))
        result.append((name, oid(parts[1].decode('ascii')), parts[2] == b'*'))
        if len(result) > 500: raise ValueError('500 Git branch/tag filters limit reached')
    return result


def graph_rows(commits):
    """Topological lanes and explicit parent IDs; edges remain correct at pages."""
    lanes, result = [], []
    for commit in commits:
        if commit.hash not in lanes: lanes.append(commit.hash)
        lane = lanes.index(commit.hash)
        glyph = ' '.join('●' if index == lane else '│' for index in range(len(lanes)))
        lanes.pop(lane)
        for parent in reversed(commit.parents):
            if parent not in lanes: lanes.insert(lane, parent)
        # Lane glyphs are hints; explicit parents describe merges/page boundaries.
        result.append((glyph, commit))
    return result


def parse_detail(data):
    rows = records(data, 6, 1)
    if len(rows) != 1: raise ValueError('Commit details missing')
    hash, parents, author, email, timestamp, message = rows[0]
    time = int(timestamp)
    if not 0 <= time <= 32503680000: raise ValueError('Invalid commit timestamp')
    date = datetime.fromtimestamp(time, timezone.utc).isoformat()
    parent_ids = tuple(oid(item.decode('ascii')) for item in parents.split())
    return (f'Commit: {oid(hash.decode("ascii"))}\nAuthor: {author.decode("utf-8", "replace")} '
            f'<{email.decode("utf-8", "replace")}>\nDate: {date}\nParents: {" ".join(parent_ids) or "(root commit)"}'
            f'\n\n{message.decode("utf-8", "replace")}')


def parse_files(data):
    if len(data) > 8 * 1024 * 1024: raise ValueError('Commit files exceed limit')
    if data and not data.endswith(b'\0'): raise ValueError('Truncated commit files')
    fields, result, index = data.split(b'\0')[:-1], [], 0
    while index < len(fields):
        status = fields[index].decode('ascii'); index += 1
        if not re.fullmatch(r'[AMDTUXB]|[RC][0-9]{1,3}', status): raise ValueError('Invalid changed-file status')
        count = 2 if status[0] in 'RC' else 1
        if index + count > len(fields): raise ValueError('Truncated changed-file paths')
        paths = tuple(git_path(value) for value in fields[index:index+count]); index += count
        result.append((status, paths[-1], paths[0] if count == 2 else None))
        if len(result) > 20000: raise ValueError('Commit file-count limit exceeded')
    return result


class History:
    def __init__(self, api, repo, execute, initial_commit=None):
        self.api, self.repo, self.execute = api, repo, execute
        self.initial_commit = initial_commit
        self.commits, self.refs, self.filter = [], [], 'all'
        self.serial, self.selection, self.timer = 0, 0, None
        self.busy, self.has_more, self.connected = False, False, True
        devices = {device.id: device for device in api.devices()}
        owner = devices[repo.device_id].display_name
        self.title = 'Git History — ' + owner + ':' + repo.root
        self.view = api.open_browser(repo.device_id, self.title, self.reload, self.more)
        self.reload()

    @property
    def closed(self): return self.view.closed

    def available(self):
        device = next((device for device in self.api.devices() if device.id == self.repo.device_id), None)
        return bool(device and (device.type == 'local' or device.state == 'connected'))

    def context(self):
        connected = self.available()
        if not connected:
            if self.connected: self.serial += 1; self.selection += 1; self.busy = False
            self.connected = False
            self.view.state('SSH disconnected or device removed — cached history retained.', connected=False)
        elif not self.connected:
            self.connected = True
            self.reload()

    def reload(self):
        if self.closed: return
        if not self.available(): self.context(); return
        self.serial += 1; token = self.serial
        self.busy = True
        self.view.state('Loading branches and Git history…')
        def refs(data, error):
            if self.closed or token != self.serial: return
            self.busy = False
            if error: self.view.state('Git history unavailable: ' + str(error)); return
            try: self.refs = parse_refs(data)
            except (ValueError, UnicodeError) as exc: self.view.state(str(exc)); return
            filters = [('all', 'All branches'), ('HEAD', 'HEAD')]
            filters += [(name, ('Local: ' if name.startswith('refs/heads/') else 'Remote: ' if name.startswith('refs/remotes/') else 'Tag: ')
                         + name.split('/', 2)[2] + (' — HEAD' if head else '')) for name, _, head in self.refs]
            if self.filter not in {key for key, _ in filters}: self.filter = 'all'
            self.view.filters(filters, self.choose_filter)
            self.load()
        self.execute(self.repo, ('for-each-ref', '--sort=refname', '--format=' + REF_FORMAT, 'refs/heads', 'refs/remotes', 'refs/tags'), refs)

    def choose_filter(self, key):
        if key not in ('all', 'HEAD'): refname(key)
        self.filter = key
        self.load()

    def load(self, append=False):
        if self.closed: return
        if not self.available(): self.context(); return
        self.serial += 1; token = self.serial
        self.busy = True
        self.selection += 1
        self.view.state('Loading Git history…')
        if not append: self.commits = []; self.view.rows([]); self.view.detail('Select a commit to inspect its message, parents and changed files.')
        count = len(self.commits)
        revisions = ('--all',) if self.filter == 'all' else ('--end-of-options', self.filter)
        if self.filter == 'all' and self.repo.status and self.repo.status.head: revisions += ('HEAD',)
        command = ('log', '-z', '--no-show-signature', '--decorate=short', '--topo-order', '--date-order', '--max-count=101', '--skip=' + str(count), '--format=' + LOG_FORMAT, *revisions, '--')
        def done(data, error):
            if self.closed or token != self.serial: return
            self.busy = False
            if error:
                self.view.state('Git history unavailable: ' + str(error)); return
            try: batch = parse_log(data)
            except (ValueError, UnicodeError) as exc: self.view.state(str(exc)); return
            self.has_more = len(batch) > 100 and count + 100 < 1000
            self.commits.extend(batch[:100])
            rows = [{'text': glyph + ' ' + commit.hash[:8] + '  ' + commit.summary + '  — ' + commit.author + ' · ' + commit.date
                     + (' [' + commit.refs + ']' if commit.refs else ''),
                     'select': lambda commit=commit: self.select(commit)} for glyph, commit in graph_rows(self.commits)]
            self.view.rows(rows)
            branch = self.repo.status.branch if self.repo.status else 'HEAD'
            self.view.state(branch + ' · ' + str(len(rows)) + ' commits' if rows else 'No commits yet.', more=self.has_more)
            if rows and not append:
                selected = self.initial_commit or self.commits[0]
                self.initial_commit = None
                self.select(selected)
        self.execute(self.repo, command, done)

    def more(self):
        if not self.busy and self.has_more: self.load(True)

    def select(self, commit):
        if self.closed or not self.available(): return
        self.selection += 1; token = self.selection
        self.view.detail('Loading commit ' + commit.hash[:8] + '…')
        results = {}
        def part(name, data, error):
            if self.closed or token != self.selection: return
            if error: self.view.detail('Commit details unavailable: ' + str(error)); self.selection += 1; return
            results[name] = data
            if len(results) != 2: return
            try:
                detail, files = parse_detail(results['detail']), parse_files(results['files'])
                if len(files) > 500: detail += f'\n\nShowing 500 of {len(files)} changed files.'
                actions = [{'text': status + ' ' + path + (' ← ' + original if original else ''),
                            'select': lambda path=path, original=original: self.diff(commit, path, original)}
                           for status, path, original in files[:500]]
                self.view.detail(detail, actions)
            except (ValueError, UnicodeError) as exc: self.view.detail(str(exc))
        self.execute(self.repo, ('show', '-s', '-z', '--no-show-signature', '--format=' + DETAIL_FORMAT, commit.hash, '--'), lambda data, error: part('detail', data, error))
        base = (commit.parents[0], commit.hash) if commit.parents else ('--root', commit.hash)
        self.execute(self.repo, ('diff-tree', '--no-commit-id', '--name-status', '-r', '-z', '-M', *base, '--'), lambda data, error: part('files', data, error))

    def diff(self, commit, path, original=None):
        if self.closed or not self.available(): return
        paths = (original, path) if original else (path,)
        flags = ('--no-color', '--no-ext-diff', '--no-textconv', '--ignore-submodules')
        command = ('diff', *flags, commit.parents[0], commit.hash, '--', *paths) if commit.parents else ('show', '--no-show-signature', '--format=', '--root', *flags, commit.hash, '--', *paths)
        def done(data, error):
            if self.closed: return
            if not self.available(): self.context(); return
            if error: self.view.state('Git diff unavailable: ' + str(error))
            else:
                title = 'Commit: ' + commit.hash[:8] + ' — ' + path
                try:
                    if b'\0' in data: raise UnicodeError('Binary patch')
                    patch = data.decode('utf-8') or 'No textual diff (possibly binary).'
                except UnicodeError: patch = 'Binary files old and new differ'
                if hasattr(self.api, 'ui'):
                    from ..transfers import FileLocation
                    self.api.ui.open_diff(self.repo.device_id, title, patch,
                        location=FileLocation(self.repo.device_id, posixpath.join(self.repo.root, path)),
                        repository=self.repo.root, historical=True)
                else: self.api.open_text(self.repo.device_id, title, patch)
        self.execute(self.repo, command, done)

    def invalidate(self):
        if self.closed: return
        self.api.cancel_deferred(self.timer)
        self.timer = self.api.defer(1000, self.reload)

    def stop(self):
        self.serial += 1; self.selection += 1
        self.api.cancel_deferred(self.timer)

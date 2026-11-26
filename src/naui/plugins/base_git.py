"""Source Control orchestration through public device, file and view APIs.

Porcelain v2 -z records are parsed as bytes, including the extra ORIGINAL_PATH
record for renames. Paths are data; no Git output becomes executable code.
"""
from dataclasses import dataclass, field
import posixpath

from ..transfers import FileLocation

from .git_service import GIT, GitChange, GitStatus, parse_status, git_path, repository_path, GitService


@dataclass
class Repository:
    device_id: str
    root: str
    git_dir: str | None = None
    status: GitStatus | None = None
    busy: bool = False
    again: bool = False
    timer: object = None
    watches: list = field(default_factory=list)
    connected: bool = True
    active: bool = True
    error: str | None = None
    operation: str | None = None
    operation_error: str | None = None
    draft: str = ''
    outgoing: tuple = ()
    outgoing_key: tuple | None = None
    outgoing_error: str | None = None
    outgoing_token: int = 0
    graph: tuple = ()
    graph_key: tuple | None = None
    graph_error: str | None = None
    graph_token: int = 0


class GitPlugin:
    def __init__(self, api):
        self.api, self.repositories, self.detected = api, {}, {}
        self.service = GitService(api)
        self.device_states = {}
        self.detect_tokens = {}
        self.detection_errors, self.histories = {}, {}
        self.rendered = None
        self.focused_repo = None
        self.observer, self.closed = None, False
        api.on_workspace_changed(self.contexts)
        api.add_command('refresh', 'Refresh Source Control', self.refresh_all)
        api.add_command('history', 'Git History', self.history)
        api.add_command('open_source_control', 'Open Source Control', lambda: api.ui.focus_sidebar('status'))
        for action, title in [('stage', 'Stage Current Git File'), ('unstage', 'Unstage Current Git File'), ('open_diff', 'Open Current Git Diff')]:
            api.add_command(action, title, lambda action=action: self.current_action(action), context='git', enabled=lambda action=action: self.action_enabled(action))

    def contexts(self):
        if self.closed: return
        if self.observer is None:
            self.observer = self.api.files.observe(self.changed)
            self.render()
        for history in self.histories.values():
            if not history.closed: history.context()
        for device in self.api.devices():
            connected = device.type == 'local' or device.state == 'connected'
            if connected and self.device_states.get(device.id) is False:
                self.detected = {key: root for key, root in self.detected.items() if key[0] != device.id or root}
                self.detect_tokens = {key: token for key, token in self.detect_tokens.items() if key in self.detected}
            self.device_states[device.id] = connected
            paths = set(device.roots + ((device.current_path,) if device.current_path else ()))
            for (identity, _), repo in self.repositories.items():
                active = any(path == repo.root or path.startswith(repo.root.rstrip('/') + '/') for path in paths)
                active = active or any(repo.root.startswith(root.rstrip('/') + '/') for root in device.roots)
                if identity == device.id and (repo.connected != connected or repo.active != active):
                    repo.connected, repo.active = connected, active
                    if not connected or not active:
                        for watch in repo.watches: watch.close()
                        repo.watches.clear()
                        self.api.cancel_deferred(repo.timer)
                        repo.timer = None
                    else:
                        self.watch_repository(repo)
                        self.schedule(repo)
                    self.render()
            if not connected: continue
            for path in paths:
                key = device.id, path
                if key in self.detected: continue
                if len(self.detected) >= 256:
                    old = next(iter(self.detected))
                    self.detected.pop(old); self.detect_tokens.pop(old, None)
                    self.detection_errors.pop(old, None)
                self.detected[key] = None
                token = self.detect_tokens[key] = object()
                def detected(data, error, key=key, token=token):
                    if self.closed or self.detect_tokens.get(key) is not token: return
                    if error:
                        self.detected[key] = False
                        self.detection_errors[key] = str(error)
                        self.render()
                        return
                    try: root = repository_path(data)
                    except (ValueError, UnicodeError) as exc:
                        self.detected[key] = False
                        self.detection_errors[key] = 'Repository unavailable: ' + str(exc)
                        self.render()
                        return
                    self.detected[key] = root
                    self.detection_errors.pop(key, None)
                    repo_key = key[0], root
                    if repo_key not in self.repositories:
                        if len(self.repositories) >= 24:
                            self.api.message('Source Control is limited to 24 repositories per launch.')
                            return
                        repo = self.repositories[repo_key] = Repository(key[0], root)
                        self.watch_repository(repo)
                        self.schedule(repo, 0)
                        self.render()
                        def metadata(data, error, repo=repo):
                            if not error:
                                try: repo.git_dir = repository_path(data)
                                except (ValueError, UnicodeError): return
                                self.watch_repository(repo)
                        self.execute(repo, ('rev-parse', '--absolute-git-dir'), metadata)
                self.api.commands.execute(device_id=device.id, cwd=path,
                    command=(*GIT, 'rev-parse', '--show-toplevel'), callback=detected)
        self.render()

    def execute(self, repo, command, callback):
        self.service.execute(repo, command, callback)

    def watch_repository(self, repo):
        for watch in repo.watches: watch.close()
        repo.watches.clear()
        if self.closed or not repo.connected or not repo.active: return
        paths = [repo.root]
        if repo.git_dir:
            paths += [repo.git_dir, posixpath.join(repo.git_dir, 'refs'), posixpath.join(repo.git_dir, 'refs/heads'), posixpath.join(repo.git_dir, 'refs/remotes')]
        for path in paths:
            try:
                repo.watches.append(self.api.files.watch(device_id=repo.device_id, path=path, callback=lambda event: None))
            except (OSError, ValueError): pass

    def changed(self, event):
        for repo in self.repositories.values():
            if repo.device_id == event.device_id and repo.connected and repo.active and any(
                    event.path == path or event.path.startswith(path.rstrip('/') + '/')
                    for path in (repo.root, repo.git_dir) if path):
                self.schedule(repo)
                if repo.git_dir and (posixpath.basename(event.path) in {'HEAD', 'packed-refs'}
                        or event.path.startswith(repo.git_dir.rstrip('/') + '/refs')):
                    history = self.histories.get((repo.device_id, repo.root))
                    if history and not history.closed: history.invalidate()
        if posixpath.basename(event.path) == '.git':
            self.detected = {key: value for key, value in self.detected.items() if key[0] != event.device_id or value is not False}
            self.contexts()

    def schedule(self, repo, delay=500):
        if self.closed or not repo.connected or not repo.active: return
        if repo.timer is not None: self.api.cancel_deferred(repo.timer)
        repo.timer = self.api.defer(delay, lambda: self.refresh(repo))

    def refresh(self, repo):
        repo.timer = None
        if self.closed or not repo.connected or not repo.active: return
        if repo.busy or repo.operation:
            repo.again = True
            return
        repo.busy = True
        if repo.status is None: self.render()
        def done(data, error):
            repo.busy = False
            if self.closed: return
            previous = repo.status, repo.error
            if error: repo.error = str(error)
            else:
                try:
                    repo.status, repo.error = parse_status(data), None
                    statuses = {posixpath.join(repo.root, change.path): change.decoration for change in repo.status.changes}
                    if previous[0] != repo.status:
                        self.api.decorate_files(repo.device_id, repo.root, repo.status.branch, statuses)
                        history = self.histories.get((repo.device_id, repo.root))
                        if history and not history.closed and previous[0] and (previous[0].head, previous[0].branch) != (repo.status.head, repo.status.branch):
                            history.invalidate()
                except (ValueError, UnicodeError) as exc: repo.error = str(exc)
            if previous != (repo.status, repo.error): self.render()
            if not repo.error:
                self.load_outgoing(repo)
                if self.selected_repo()[1] is repo: self.load_graph(repo)
            if repo.again:
                repo.again = False
                self.schedule(repo)
        self.service.status(repo, done)

    def refresh_all(self):
        self.detected = {key: root for key, root in self.detected.items() if root is not False}
        self.detection_errors.clear()
        self.contexts()
        for repo in self.repositories.values():
            repo.outgoing_key = None
            repo.graph_key = None
            repo.operation_error = None
            self.schedule(repo, 0)

    def selected_repo(self):
        devices = self.api.devices()
        chosen = self.repositories.get(self.focused_repo)
        if chosen and chosen.active:
            owner = next((d for d in devices if d.id == chosen.device_id), None)
            if owner: return owner, chosen
        device = next((device for device in devices if getattr(device, 'active', False)), next(iter(devices), None))
        if device is None: return None, None
        path = device.current_path or '/'
        if hasattr(self.api, 'selected_location'):
            selected = self.api.selected_location()
            if selected and selected.device_id == device.id: path = selected.path
        candidates = [repo for repo in self.repositories.values() if repo.device_id == device.id and repo.active]
        matching = [repo for repo in candidates if path == repo.root or path.startswith(repo.root.rstrip('/') + '/')]
        # A different pinned root on the same device must not inherit the
        # previously selected repository's branch, changes or history.
        return device, max(matching, key=lambda repo: len(repo.root)) if matching else None

    def choose_repository(self, repo):
        self.focused_repo = (repo.device_id, repo.root)
        self.render()
        if repo.connected and repo.status: self.load_graph(repo)

    def load_graph(self, repo):
        status = repo.status
        if not status or not repo.connected: return
        key = (status.head, status.branch)
        if repo.graph_key == key: return
        repo.graph_key = key
        repo.graph_token += 1; token = repo.graph_token
        repo.graph, repo.graph_error = (), None
        if not status.head: self.render(); return
        from .git_history import LOG_FORMAT, parse_log
        def done(data, error):
            if self.closed or token != repo.graph_token or not repo.connected: return
            if error: repo.graph_error = 'History unavailable. Use Refresh to retry.'
            else:
                try: repo.graph = tuple(parse_log(data, maximum=8))
                except (ValueError, UnicodeError): repo.graph_error = 'History could not be read.'
            self.render()
        self.execute(repo, ('log', '-z', '--no-show-signature', '--decorate=short',
                           '--topo-order', '--max-count=8', '--format=' + LOG_FORMAT, 'HEAD', '--'), done)

    def history(self, repo=None, commit=None):
        repo = repo or self.selected_repo()[1]
        if repo is None:
            self.api.message('Select a Git repository in the workspace first.'); return
        key = repo.device_id, repo.root
        previous = self.histories.get(key)
        if previous and not previous.closed:
            if commit: previous.select(commit)
            else: previous.reload()
            previous.view.present()
            return
        from .git_history import History
        self.histories[key] = History(self.api, repo, self.execute, initial_commit=commit)

    def load_outgoing(self, repo):
        status = repo.status
        if not status: return
        key = (status.head, status.upstream, status.ahead, status.behind)
        if repo.outgoing_key == key: return
        repo.outgoing_key = key
        repo.outgoing_token += 1; token = repo.outgoing_token
        repo.outgoing, repo.outgoing_error = (), None
        if not status.upstream or not status.ahead:
            self.render(); return
        def done(data, error):
            if self.closed or token != repo.outgoing_token or not repo.connected: return
            if error: repo.outgoing_error = str(error)
            else:
                from .git_history import parse_log
                try: repo.outgoing = tuple(parse_log(data, maximum=21)[:20])
                except (ValueError, UnicodeError) as exc: repo.outgoing_error = str(exc)
            self.render()
        self.service.outgoing(repo, done)

    def current_change(self):
        context = self.api.ui.current_diff()
        if not context or not context.get('location') or context.get('historical'): return None
        repo = self.repositories.get((context['device_id'], context.get('repository')))
        if not repo or not repo.status or not repo.connected or repo.operation: return None
        path = posixpath.relpath(context['location'].path, repo.root)
        staged = bool(context.get('staged'))
        candidates = [c for c in repo.status.changes if c.path == path]
        change = next((c for c in candidates if
                       (c.index not in {'.', '?', '!'} if staged else c.worktree != '.')),
                      candidates[0] if len(candidates) == 1 else None)
        return (repo, change, bool(context.get('staged'))) if change else None

    def action_enabled(self, action):
        current = self.current_change()
        if not current: return False
        _, change, _ = current
        if action == 'unstage': return not change.conflicted and change.index not in {'.', '?', '!'}
        if action == 'stage': return change.conflicted or change.worktree not in {'.', '!'}
        return True

    def current_action(self, action):
        current = self.current_change()
        if not current or not self.action_enabled(action): return
        repo, change, staged = current
        if action == 'open_diff': self.diff(repo, change, staged)
        else: self.mutate(repo, action, change)

    def mutate(self, repo, action, change=None, message=None):
        if self.closed or not repo.active or not repo.connected: self.api.message('Repository unavailable or SSH disconnected.'); return
        if repo.operation or repo.busy: self.api.message('Git is busy; wait for the current operation.'); return
        repo.operation = action
        repo.operation_error = None
        self.render()
        def completed(data, error):
            repo.operation = None
            if self.closed: return
            if error:
                repo.operation_error = str(error); self.api.message('Git ' + action + ' failed: ' + str(error))
            elif action == 'commit': repo.draft = ''
            repo.outgoing_key = None
            self.render()
            self.schedule(repo, 0)
            if not error and action in {'commit', 'pull', 'fetch', 'push'}:
                history = self.histories.get((repo.device_id, repo.root))
                if history and not history.closed: history.invalidate()
        def fresh(data, error):
            if self.closed: return
            if error: completed(None, error); return
            try:
                current = parse_status(data)
                if change:
                    if change not in current.changes: raise ValueError('File Git state changed. Refresh and try again.')
                repo.status = current
                self.service.operation(repo, action, completed, change=change, message=message)
            except (ValueError, UnicodeError) as exc: completed(None, exc)
        self.service.status(repo, fresh)

    def discard(self, repo, change):
        if change.directory:
            self.api.message('Discard refuses directories.'); return
        if change.conflicted:
            self.api.message('Resolve merge conflicts in the editor; automatic discard is unavailable.'); return
        location = FileLocation(repo.device_id, posixpath.join(repo.root, change.path))
        message = ('This permanently deletes the untracked file. Directories will not be removed recursively.'
                   if change.index == '?' else 'This permanently removes unstaged changes, restoring the index version. Staged changes remain intact.')
        self.api.confirm('Discard ' + change.path + '?', repo.root + '\n' + message,
                         'Delete File' if change.index == '?' else 'Discard', lambda: self.mutate(repo, 'discard', change))

    def network(self, repo, action):
        # Network actions are explicit; never fetch in a status refresh.
        self.api.confirm(action.title() + ' — ' + posixpath.basename(repo.root),
            'Repository: ' + repo.device_id + ':' + repo.root + '\n' +
            ('Pull uses fast-forward only and never discards working files.' if action == 'pull' else
             'Push the current branch to its configured upstream.' if action == 'push' else 'Fetch configured remote refs without changing working files.'),
            action.title(), lambda: self.mutate(repo, action))

    def file_row(self, repo, change, staged):
        location = FileLocation(repo.device_id, posixpath.join(repo.root, change.path))
        menu = [('Open Diff', lambda: self.diff(repo, change, staged)), ('Open File', lambda: self.api.open(location)),
                ('Unstage Changes' if staged else 'Stage Changes', lambda: self.mutate(repo, 'unstage' if staged else 'stage', change))]
        if not staged and not change.conflicted and not change.directory: menu.append(('Discard Changes…', lambda: self.discard(repo, change)))
        menu += [('Copy Path', lambda: self.api.ui.copy_text(location.path)),
                 ('Reveal in File Tree', lambda: self.api.files.reveal(location))]
        code = change.code(staged)
        names = {'M': 'Modified', 'A': 'Added', 'D': 'Deleted', 'R': 'Renamed', 'C': 'Copied', '?': 'Untracked', '!': 'Conflicted', 'T': 'Type changed'}
        name = (change.original + ' → ' if change.original else '') + change.path
        return {'text': posixpath.basename(change.path), 'detail': posixpath.dirname(change.path),
                'icon': 'text-x-script-symbolic' if change.path.endswith('.py') else 'text-x-generic-symbolic',
                'badge': 'U' if code == '?' else code, 'tooltip': names.get(code, code) + ' · ' + location.device_id + ':' + location.path + (' · from ' + change.original if change.original else ''),
                'select': lambda: self.diff(repo, change, staged), 'menu': menu,
                'actions': [('Unstage Changes' if staged else 'Stage Changes',
                             lambda: self.mutate(repo, 'unstage' if staged else 'stage', change))]}

    def render(self):
        device, selected = self.selected_repo()
        devices = {d.id: d for d in self.api.devices()}
        repos = sorted((r for r in self.repositories.values() if r.active and r.device_id in devices),
                       key=lambda r: (r is not selected, r.device_id, r.root))
        state = (self.focused_repo, device.id if device else None, device.state if device else None,
                 tuple((r.device_id, r.root, r.connected, r.status, r.error, r.operation, r.operation_error, r.outgoing, r.outgoing_error, r.graph, r.graph_error, r.busy) for r in repos),
                 tuple(sorted(self.detection_errors.items())), tuple(sorted((key, value is None) for key, value in self.detected.items())))
        if state == self.rendered: return
        self.rendered = state
        rows = [{'text': '', 'header': True, 'actions': [('History', lambda: self.history()), ('Refresh', self.refresh_all)]}]
        if device is None: rows.append({'text': 'No workspace device'})
        elif device.type == 'ssh' and device.state != 'connected':
            rows.append({'text': device.display_name + (' — SSH connecting…' if device.state == 'connecting' else ' — SSH disconnected')})
        elif selected is None:
            errors = [error for (identity, _), error in self.detection_errors.items() if identity == device.id]
            pending = any(identity == device.id and root is None for (identity, _), root in self.detected.items())
            error = next((error for error in errors if 'not a git repository' not in error.lower()), None)
            if error: rows.append({'text': 'Git unavailable on ' + device.display_name + ': ' + error})
            elif pending: rows.append({'text': 'Loading Git repository…'})
            else: rows.append({'text': 'Not a Git repository'})
        total = sum(len({c.path for c in r.status.changes if c.index != '!'}) for r in repos if r.status)
        repository_rows = []
        for repo in repos:
            status = repo.status
            branch = status.branch if status else 'Loading…'
            if not repo.connected: branch = 'Disconnected'
            choices = [('History', lambda repo=repo: self.history(repo)), ('Refresh', lambda repo=repo: self.schedule(repo, 0))]
            if repo.connected:
                choices += [('Stage All', lambda repo=repo: self.mutate(repo, 'stage')),
                            ('Unstage All', lambda repo=repo: self.mutate(repo, 'unstage')),
                            ('Fetch…', lambda repo=repo: self.network(repo, 'fetch'))]
                if status and status.upstream:
                    choices += [('Pull…', lambda repo=repo: self.network(repo, 'pull')), ('Push…', lambda repo=repo: self.network(repo, 'push'))]
            repository_rows.append({'text': posixpath.basename(repo.root) or repo.root,
                'detail': branch, 'icon': 'folder-symbolic', 'selected': repo is selected,
                'tooltip': devices[repo.device_id].display_name + ':' + repo.root,
                'select': lambda repo=repo: self.choose_repository(repo), 'menu': choices})
        if repos: rows.append({'text': 'REPOSITORIES', 'id': 'repositories', 'children': repository_rows})
        repo = selected
        if repo:
            status = repo.status
            children = []
            if not repo.connected: children.append({'text': 'SSH disconnected; cached status'})
            if repo.error: children.append({'text': 'Git command failed: ' + repo.error})
            if repo.operation_error: children.append({'text': 'Last Git operation failed: ' + repo.operation_error})
            if repo.operation: children.append({'text': repo.operation.title() + ' in progress…'})
            if status and repo.connected:
                branch = 'detached HEAD' if status.branch == '(detached)' else status.branch
                staged_count = sum(not c.conflicted and c.index not in {'.', '?', '!'} for c in status.changes)
                children.append({'text': '', 'input': {'value': repo.draft,
                    'placeholder': 'Message (commit to ' + branch + ')',
                    'changed': lambda text, repo=repo: setattr(repo, 'draft', text)}})
                children.append({'text': 'Commit staged changes' if staged_count else 'Commit (stage changes first)',
                    'primary': lambda repo=repo: self.mutate(repo, 'commit', message=repo.draft),
                    'enabled': bool(staged_count) and not repo.operation and not repo.busy})
                for name, predicate, staged in [('Conflicts', lambda c: c.conflicted, False),
                        ('Staged Changes', lambda c: not c.conflicted and c.index not in {'.', '?', '!'}, True),
                        ('Changes', lambda c: not c.conflicted and c.index != '!' and (c.worktree != '.' or c.index == '?'), False)]:
                    changes = [c for c in status.changes if predicate(c)]
                    if not changes: continue
                    items = [self.file_row(repo, change, staged) for change in changes[:150]]
                    if len(changes) > 150: items.append({'text': f'{len(changes)-150} more files; use Git terminal for the full list'})
                    children.append({'text': name + '  ' + str(len(changes)), 'id': repo.device_id + ':' + repo.root + ':' + name, 'children': items,
                        'actions': [('Unstage All' if staged else 'Stage All',
                                     lambda repo=repo, staged=staged: self.mutate(repo, 'unstage' if staged else 'stage'))] if name != 'Conflicts' else []})
                if not status.changes: children.append({'text': 'No local changes'})
                counts = (f'↑ {status.ahead} outgoing · ↓ {status.behind} incoming' if status.upstream else 'No upstream configured')
                children.append({'text': counts, 'tooltip': status.upstream or counts})
                if repo.outgoing:
                    children.append({'text': 'Outgoing commits  ' + str(status.ahead), 'expanded': False,
                        'id': repo.device_id + ':' + repo.root + ':outgoing',
                        'children': [{'text': c.summary, 'detail': c.hash[:7], 'select': lambda c=c: self.history(repo, c)} for c in repo.outgoing]})
            elif not status: children.append({'text': 'Loading Git status…'})
            rows.append({'text': 'CHANGES', 'id': 'selected-changes', 'children': children})
            from .git_history import graph_rows
            history_rows = [{'text': glyph + '  ' + commit.summary, 'detail': commit.refs or commit.author,
                'tooltip': commit.hash + ' · ' + commit.author + ' · ' + commit.date,
                'select': lambda commit=commit, repo=repo: self.history(repo, commit)} for glyph, commit in graph_rows(repo.graph)]
            if not history_rows: history_rows.append({'text': repo.graph_error or ('Loading history…' if status and status.head else 'No commits yet')})
            history_rows.append({'text': 'Open full history', 'select': lambda repo=repo: self.history(repo)})
            rows.append({'text': 'GRAPH', 'id': 'git-graph', 'footer': True, 'children': history_rows})
        self.api.sidebar('status', 'SOURCE CONTROL' + (f' · {total}' if total else ''), rows)
        ui = getattr(self.api, 'ui', None)
        if ui and hasattr(ui, 'set_sidebar_badge'): ui.set_sidebar_badge('status', total)

    def diff(self, repo, change, staged=False):
        if self.closed or not repo.connected: self.api.message('SSH disconnected or repository unavailable.'); return
        if change.directory:
            self.api.message('Directory entry: reveal it in the file tree or open its repository history.'); return
        if change.conflicted:
            self.api.open(FileLocation(repo.device_id, posixpath.join(repo.root, change.path))); return
        title = 'Diff: ' + change.path + (' [staged]' if staged else ' [working]') + ' — ' + posixpath.basename(repo.root)
        location = FileLocation(repo.device_id, posixpath.join(repo.root, change.path))
        def show(data, error):
            if self.closed or not repo.connected: return
            if error: self.api.message(str(error)); return
            if isinstance(data, bytes):
                try:
                    if b'\0' in data: raise UnicodeError('Binary patch')
                    patch = data.decode('utf-8')
                except UnicodeError: patch = 'Binary files old and new differ'
            else: patch = data
            self.api.ui.open_diff(repo.device_id, title, patch or 'No textual diff.', location=location, repository=repo.root, staged=staged,
                hunk_action=lambda: self.stage_hunk(repo, change, staged, patch))
        if change.index == '?':
            def text(content, error):
                if error: show(None, error); return
                from ..diff_model import text_diff, HARD_LINES
                if content.count('\n') > HARD_LINES:
                    show('Large diff: untracked file exceeds the line limit. Use Open File.', None); return
                # Preserve missing-newline markers without splitting millions
                # of lines on the UI thread.

                patch = text_diff('', content, '/dev/null', change.path)
                show(patch, None)
            def probe(sample, error):
                if error: show(None, error); return
                from ..viewer_registry import looks_like_text
                if not looks_like_text(sample): show('Binary files /dev/null and ' + change.path + ' differ', None)
                else: self.api.read_text(location, text)
            def properties(entry, error):
                if error: show(None, error); return
                if entry.is_directory: show(None, ValueError('Directories cannot be previewed as file diffs')); return
                if entry.size > 8 * 1024 * 1024:
                    show('Large diff: untracked file exceeds 8 MiB. Use Open File.', None); return
                self.api.files.probe(location, probe)
            self.api.properties(location, properties)
        else: self.service.diff(repo, change, staged, show)

    def stage_hunk(self, repo, change, staged, reviewed):
        from ..git_hunks import select_hunk
        context = self.api.ui.current_diff()
        if self.closed or not repo.connected or not context: return
        try: patch = select_hunk(reviewed, context.get('selected_hunk', -1)).encode('utf-8')
        except ValueError as error: self.api.message(str(error)); return
        def checked(data, error):
            if self.closed or not repo.connected: return
            if error: self.api.message(str(error)); return
            if data != reviewed.encode('utf-8'):
                self.api.message('Diff changed. Reopen it and review the change before staging.'); return
            command = ('git', 'apply', '--cached', '--whitespace=nowarn', *(('--reverse',) if staged else ()), '-')
            def applied(_, error):
                if self.closed: return
                self.api.message(str(error) if error else 'Change unstaged. Reopen the diff to review remaining changes.' if staged else 'Change staged. Reopen the diff to review remaining changes.')
                self.refresh_all()
            self.api.commands.execute(device_id=repo.device_id, cwd=repo.root, command=command, input_data=patch, callback=applied)
        self.service.diff(repo, change, staged, checked)

    def stop(self):
        if self.closed: return
        self.closed = True
        for repo in self.repositories.values():
            self.api.cancel_deferred(repo.timer)
            repo.timer = None
            repo.outgoing_token += 1
            repo.graph_token += 1
        for history in self.histories.values(): history.stop()
        for repo in self.repositories.values():
            for watch in repo.watches: watch.close()


def start(api): return GitPlugin(api)

"""Real Git merge/rename/history fixtures, device routing and presentation rules."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from naui.commands import local_command
from naui.plugins.base_git import GIT, GitPlugin, Repository, parse_status
from naui.plugins.git_history import (LOG_FORMAT, DETAIL_FORMAT, REF_FORMAT, History,
                                     parse_log, parse_refs, parse_detail, parse_files, graph_rows, refname)
from naui.plugin_api import DeviceInfo
from naui.backends import RemoteFileBackend
from naui.sidebar_layout import root_label, indentation
from naui.ui_settings import UISettings
from test_workspace import RootedTransport


class View:
    closed = False
    def __init__(self): self.items, self.choices, self.text, self.files, self.message, self.connected = [], [], '', [], '', True
    def rows(self, items): self.items = items
    def filters(self, choices, callback): self.choices, self.filter_callback = choices, callback
    def detail(self, text, files=()): self.text, self.files = text, files
    def state(self, message, connected=True, more=False): self.message, self.connected, self.more = message, connected, more
    def present(self): pass


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'project'; self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Naui Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root/'space õ.py').write_text('first=1\n')
        self.git('add', '--', 'space õ.py'); self.git('commit', '-qm', 'Initial subject\n\nFull initial message')
        self.initial = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('checkout', '-qb', 'feature/history')
        (self.root/'feature.txt').write_text('feature')
        self.git('add', '--', 'feature.txt'); self.git('commit', '-qm', 'Feature commit')
        self.git('checkout', '-q', 'main')
        self.git('mv', '--', 'space õ.py', 'renamed õ.py'); self.git('commit', '-qm', 'Rename source')
        self.git('merge', '--no-ff', '-qm', 'Merge feature', 'feature/history')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('tag', 'v1')

    def git(self, *arguments): return local_command((*GIT, *arguments), str(self.root))

    def test_log_merge_parents_author_and_head_decorations(self):
        commits = parse_log(self.git('log', '-z', '--topo-order', '--format='+LOG_FORMAT, '--all', '--'))
        self.assertEqual(len(commits), 4)
        self.assertEqual(len(commits[0].parents), 2)
        self.assertEqual(commits[0].author, 'Naui Test')
        self.assertIn('HEAD -> main', commits[0].refs)
        self.assertIn('UTC', commits[0].date)
        graph = graph_rows(commits)
        self.assertEqual([commit for glyph, commit in graph], commits)
        self.assertTrue(any('│' in glyph for glyph, commit in graph))
        self.assertEqual(commits[-1].hash, self.initial)
        self.assertEqual(commits[-1].parents, ())
        with self.assertRaises(ValueError): parse_log(b'incomplete')
        with self.assertRaises(ValueError): parse_log(b'bad\0fields\0')

    def test_ref_parser_local_remote_tag_and_current_branch(self):
        refs = parse_refs(self.git('for-each-ref', '--format='+REF_FORMAT, 'refs/heads', 'refs/remotes', 'refs/tags'))
        self.assertIn(('refs/heads/main', self.git('rev-parse', 'HEAD').decode().strip(), True), refs)
        self.assertTrue(any(name == 'refs/heads/feature/history' for name, hash, head in refs))
        self.assertTrue(any(name == 'refs/remotes/origin/main' for name, hash, head in refs))
        self.assertTrue(any(name == 'refs/tags/v1' for name, hash, head in refs))
        for name in ('--exec=anything', 'refs/heads/x\n--evil', 'refs/heads/../x'):
            with self.assertRaises(ValueError): refname(name)
        status = parse_status(self.git('status', '--porcelain=v2', '-z', '--branch'))
        self.assertEqual(status.branch, 'main')
        self.assertEqual(status.head, self.git('rev-parse', 'HEAD').decode().strip())

    def test_commit_full_message_changed_file_and_root_diff(self):
        detail = parse_detail(self.git('show', '-s', '-z', '--format='+DETAIL_FORMAT, self.initial, '--'))
        self.assertIn('Full initial message', detail)
        self.assertIn('Parents: (root commit)', detail)
        files = parse_files(self.git('diff-tree', '--root', '--no-commit-id', '--name-status', '-r', '-z', self.initial, '--'))
        self.assertEqual(files, [('A', 'space õ.py', None)])
        rename = self.git('rev-parse', 'HEAD^1').decode().strip()
        files = parse_files(self.git('diff-tree', '--no-commit-id', '--name-status', '-r', '-z', '-M', rename, '--'))
        self.assertEqual(files, [('R100', 'renamed õ.py', 'space õ.py')])
        with self.assertRaises(ValueError): parse_files(b'M\0../outside\0')
        with self.assertRaises(ValueError): parse_files(b'R100\0one\0')

    def api(self, remote=False):
        views, opened, commands = [], [], []
        backend = RemoteFileBackend(RootedTransport('server', self.root.parent)) if remote else None
        if backend: self.addCleanup(backend.close)
        identity, path = ('ssh-test', '/project') if remote else ('local', str(self.root))
        devices = [DeviceInfo(identity, 'ssh' if remote else 'local', 'server' if remote else 'LOCAL', 'connected', path, (path,), '/', True)]
        def execute(repo, command, callback):
            commands.append((repo.device_id, repo.root, command))
            try: data = backend.run_command((*GIT, *command), repo.root) if remote else self.git(*command)
            except Exception as error: callback(None, error)
            else: callback(data, None)
        def browser(*args):
            view = View(); views.append(view); return view
        api = SimpleNamespace(devices=lambda: devices, open_browser=browser,
            open_text=lambda *args: opened.append(args), cancel_deferred=lambda *args: None,
            defer=lambda *args: None)
        repo = Repository(identity, path, status=parse_status(self.git('status', '--porcelain=v2', '-z', '--branch')))
        return api, repo, execute, views, opened, commands, devices

    def test_public_history_view_branch_filter_commit_selection_and_diff(self):
        api, repo, execute, views, opened, commands, devices = self.api()
        history = History(api, repo, execute)
        view = views[0]
        self.assertEqual(len(view.items), 4)
        self.assertIn('Merge feature', view.text)
        self.assertEqual(view.files[0]['text'], 'A feature.txt')
        view.files[0]['select']()
        self.assertEqual(opened[-1][0], 'local')
        self.assertIn('+feature', opened[-1][2])
        history.choose_filter('refs/heads/feature/history')
        self.assertEqual(len(view.items), 2)
        self.assertTrue(all('Rename source' not in row['text'] for row in view.items))
        view.items[-1]['select']()
        self.assertIn('Full initial message', view.text)
        view.files[0]['select']()
        self.assertIn('+first=1', opened[-1][2])

    def test_commit_diff_treats_filename_as_literal_not_git_pathspec(self):
        name=':(glob)*.txt'
        (self.root/name).write_text('literal file\n')
        (self.root/'other.txt').write_text('unrelated file\n')
        self.git('add','--',name,'other.txt');self.git('commit','-qm','Literal pathspec fixture')
        api,repo,execute,views,opened,commands,devices=self.api()
        history=History(api,repo,execute)
        history.diff(history.commits[0],name)
        self.assertIn('+literal file',opened[-1][2])
        self.assertNotIn('+unrelated file',opened[-1][2])

    def test_remote_history_executes_on_remote_and_reports_disconnect(self):
        api, repo, execute, views, opened, commands, devices = self.api(True)
        history = History(api, repo, execute)
        self.assertIn('server:/project', history.title)
        self.assertEqual(len(views[0].items), 4)
        views[0].files[0]['select']()
        self.assertEqual(opened[-1][0], 'ssh-test')
        self.assertTrue(all(device == 'ssh-test' and cwd == '/project' for device, cwd, command in commands))
        devices[0] = DeviceInfo('ssh-test', 'ssh', 'server', 'disconnected')
        before = len(commands)
        history.context(); history.reload()
        self.assertFalse(views[0].connected)
        self.assertIn('disconnected', views[0].message)
        self.assertEqual(len(commands), before)
        views[0].files[0]['select']()
        self.assertEqual(len(commands), before)

    def test_unborn_repo_has_discoverable_empty_history(self):
        empty = self.root.parent/'empty'; empty.mkdir()
        local_command((*GIT, 'init', '-q'), str(empty))
        api, repo, execute, views, opened, commands, devices = self.api()
        repo.root, repo.status = str(empty), parse_status(local_command((*GIT, 'status', '--porcelain=v2', '-z', '--branch'), str(empty)))
        def command(repo, args, callback): callback(local_command((*GIT, *args), repo.root), None)
        history = History(api, repo, command)
        self.assertEqual(views[0].items, [])
        self.assertEqual(views[0].message, 'No commits yet.')

    def test_history_error_and_late_callback_are_visible_or_ignored(self):
        api, repo, execute, views, opened, commands, devices = self.api()
        history = History(api, repo, execute)
        callbacks = []
        history.execute = lambda repo, args, callback: callbacks.append(callback)
        history.reload()
        callbacks[-1](None, RuntimeError('git not found'))
        self.assertIn('git not found', views[0].message)
        history.reload(); pending = callbacks[-1]
        views[0].closed = True
        previous = views[0].message
        pending(b'', None)
        self.assertEqual(views[0].message, previous)


class SidebarModelTests(unittest.TestCase):
    def test_sidebar_widths_and_capped_indent_and_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = UISettings(Path(directory)/'ui.json')
            for width in (180, 220, 280, 350):
                settings.save(width)
                self.assertEqual(UISettings(settings.path).width, width)
        self.assertEqual(root_label('/home/user/long-project/'), 'long-project')
        self.assertEqual(root_label('/'), '/')
        self.assertEqual(indentation(1000), 30)
        self.assertEqual(indentation(2), 12)

    def test_source_control_header_always_exposes_history_refresh_and_device_errors(self):
        devices = [DeviceInfo('local', 'local', 'LOCAL', 'connected', '/repo', ('/repo',), active=True)]
        sections, messages = [], []
        api = SimpleNamespace(devices=lambda: devices, on_workspace_changed=lambda *args: None,
            add_command=lambda *args, **kwargs: None, sidebar=lambda *args: sections.append(args), message=messages.append)
        plugin = GitPlugin(api)
        plugin.render()
        header = sections[-1][-1][0]
        self.assertTrue(header['header'])
        self.assertEqual([name for name, callback in header['actions']], ['History', 'Refresh'])
        self.assertEqual(sections[-1][-1][1]['text'], 'Not a Git repository')
        plugin.detection_errors[('local', '/repo')] = 'git: command not found'
        plugin.render()
        self.assertIn('Git unavailable', sections[-1][-1][1]['text'])
        devices[:] = [DeviceInfo('ssh', 'ssh', 'server', 'disconnected', '/repo', active=True)]
        plugin.render()
        self.assertIn('SSH disconnected', sections[-1][-1][1]['text'])

    def test_source_control_switches_active_device_and_never_mixes_repositories(self):
        devices = [DeviceInfo('local', 'local', 'LOCAL', 'connected', '/a', active=True),
                   DeviceInfo('ssh', 'ssh', 'server', 'connected', '/b')]
        rows = []
        api = SimpleNamespace(devices=lambda: devices, on_workspace_changed=lambda *args: None,
            add_command=lambda *args, **kwargs: None, sidebar=lambda *args: rows.append(args[-1]))
        plugin = GitPlugin(api)
        plugin.repositories = {('local', '/a'): Repository('local', '/a'), ('ssh', '/b'): Repository('ssh', '/b')}
        plugin.render(); self.assertEqual(rows[-1][1]['children'][0]['text'], 'a')
        self.assertTrue(rows[-1][1]['children'][0]['selected'])
        devices[:] = [DeviceInfo('local', 'local', 'LOCAL', 'connected', '/a'), DeviceInfo('ssh', 'ssh', 'server', 'connected', '/b', active=True)]
        plugin.render(); self.assertEqual(rows[-1][1]['children'][0]['text'], 'b')
        self.assertTrue(rows[-1][1]['children'][0]['selected'])
        self.assertEqual({row['text'] for row in rows[-1][1]['children']}, {'b', 'a'})
        rows[-1][1]['children'][1]['select']()
        self.assertEqual(plugin.selected_repo()[1].root, '/a')
        self.assertIsNone(plugin.repositories[('local', '/a')].status)

    def test_commit_area_keeps_draft_and_targets_selected_repository(self):
        from naui.plugins.git_service import GitStatus, GitChange
        from unittest.mock import Mock
        device = DeviceInfo('local', 'local', 'LOCAL', 'connected', '/repo', active=True)
        rows = []
        api = SimpleNamespace(devices=lambda: [device], on_workspace_changed=lambda *args: None,
            add_command=lambda *args, **kwargs: None, sidebar=lambda *args: rows.append(args[-1]))
        plugin = GitPlugin(api)
        repo = Repository('local', '/repo', status=GitStatus('main', 0, 0,
            (GitChange('src/file.py', 'M', '.'),)), draft='Keep this message')
        plugin.repositories[('local', '/repo')] = repo
        plugin.mutate = Mock()
        plugin.render()
        area = next(row for row in rows[-1] if row['text'] == 'CHANGES')['children']
        self.assertEqual(area[0]['input']['value'], 'Keep this message')
        area[0]['input']['changed']('Reviewed staged files')
        self.assertTrue(area[1]['enabled']); area[1]['primary']()
        plugin.mutate.assert_called_once_with(repo, 'commit', message='Reviewed staged files')
        self.assertTrue(next(row for row in rows[-1] if row['text'] == 'GRAPH')['footer'])
        repo.status = GitStatus('main', 0, 0, (GitChange('src/file.py', '.', 'M'),))
        plugin.render()
        area = next(row for row in rows[-1] if row['text'] == 'CHANGES')['children']
        self.assertFalse(area[1]['enabled'])
        self.assertEqual(area[0]['input']['value'], 'Reviewed staged files')

    def test_non_git_root_on_same_device_does_not_inherit_other_root_history(self):
        device = DeviceInfo('local', 'local', 'LOCAL', 'connected', '/project', ('/project', '/other'), active=True)
        rows=[]
        api=SimpleNamespace(devices=lambda:[device], on_workspace_changed=lambda *args:None,
            selected_location=lambda:SimpleNamespace(device_id='local',path='/other'),
            add_command=lambda *args, **kwargs:None, sidebar=lambda *args:rows.append(args[-1]))
        plugin=GitPlugin(api)
        plugin.repositories[('local','/project')]=Repository('local','/project')
        plugin.render()
        self.assertIsNone(plugin.selected_repo()[1])
        self.assertEqual(rows[-1][1]['text'],'Not a Git repository')

    def test_history_refreshes_metadata_but_not_ordinary_file_edits(self):
        scheduled, invalidated=[],[]
        api=SimpleNamespace(on_workspace_changed=lambda *args:None,add_command=lambda *args, **kwargs:None)
        plugin=GitPlugin(api)
        repo=Repository('ssh','/repo',git_dir='/repo/.git')
        plugin.repositories[('ssh','/repo')]=repo
        plugin.histories[('ssh','/repo')]=SimpleNamespace(closed=False,invalidate=lambda:invalidated.append(True))
        plugin.schedule=lambda repo:scheduled.append(repo)
        plugin.changed(SimpleNamespace(device_id='ssh',path='/repo/source.py'))
        self.assertEqual(len(scheduled),1)
        self.assertEqual(invalidated,[])
        plugin.changed(SimpleNamespace(device_id='ssh',path='/repo/.git/HEAD'))
        self.assertEqual(invalidated,[True])
        plugin.changed(SimpleNamespace(device_id='local',path='/repo/.git/HEAD'))
        self.assertEqual(len(scheduled),2)

class PaginationAndFacadeTests(unittest.TestCase):
    def test_browser_permission_checks_precede_host_creation(self):
        from naui.plugin_api import PluginAPI
        host=SimpleNamespace(_shutting_down=False,workspace=SimpleNamespace(devices={
            'local':SimpleNamespace(type='local'), 'ssh':SimpleNamespace(type='ssh')}))
        api=PluginAPI(host,'test',set())
        with self.assertRaises(PermissionError):api.open_browser('local','History',lambda:None,lambda:None)
        api=PluginAPI(host,'test',{'views'})
        with self.assertRaises(PermissionError):api.open_browser('ssh','History',lambda:None,lambda:None)

    def test_history_paginates_and_only_refreshes_refs_when_requested(self):
        view, commands = View(), []
        device = DeviceInfo('local', 'local', 'LOCAL', 'connected', '/repo', active=True)
        api = SimpleNamespace(devices=lambda:[device], open_browser=lambda *args:view,
            cancel_deferred=lambda *args:None, defer=lambda *args:None)
        repo = Repository('local', '/repo')
        def execute(repo, command, callback):
            commands.append(command)
            if command[0]=='for-each-ref': callback(b'',None)
            elif command[0]=='log':
                skip = int(next(arg.split('=')[1] for arg in command if arg.startswith('--skip=')))
                count = 101 if not skip else 2
                callback(b''.join(b'\0'.join([f'{index+1:040x}'.encode(), b'', b'Author', b'1000', b'Subject', b''])+b'\0'
                    for index in range(skip,skip+count)),None)
            elif command[0]=='show': callback(b'\0'.join([b'1'*40,b'',b'Author',b'email',b'1000',b'Message'])+b'\0',None)
            else: callback(b'',None)
        history = History(api,repo,execute)
        self.assertEqual(len(view.items),100)
        self.assertTrue(view.more)
        history.more()
        self.assertEqual(len(view.items),102)
        self.assertFalse(view.more)
        self.assertEqual(sum(command[0]=='for-each-ref' for command in commands),1)

    def test_sidebar_render_avoids_rebuilding_identical_state(self):
        device = DeviceInfo('local','local','LOCAL','connected','/repo',active=True)
        rendered=[]
        api=SimpleNamespace(devices=lambda:[device],on_workspace_changed=lambda *args:None,
            add_command=lambda *args, **kwargs:None,sidebar=lambda *args:rendered.append(args))
        plugin=GitPlugin(api)
        plugin.render();plugin.render();plugin.render()
        self.assertEqual(len(rendered),1)

    def test_view_facade_limits_and_late_callbacks(self):
        from naui.plugin_api import BrowserHandle
        calls=[]
        view=SimpleNamespace(_shutting_down=False, set_rows=lambda rows:calls.extend(rows),close=lambda:None,
            set_detail=lambda *args:None,set_filters=lambda *args:None)
        api=SimpleNamespace(closed=False, quiescing=False, guard=lambda callback: callback,
                            _host=SimpleNamespace(_shutting_down=False))
        handle=BrowserHandle(api,view)
        invoked=[]
        handle.rows([{'text':'Commit','select':lambda:invoked.append(True)}])
        callback=calls[0]['select'];callback();self.assertEqual(invoked,[True])
        view._shutting_down=True;callback();self.assertEqual(invoked,[True])
        view._shutting_down=False
        with self.assertRaises(ValueError):handle.rows([{'text':'x','select':lambda:None}]*1001)
        with self.assertRaises(ValueError):handle.filters([('a','b')]*513,lambda *args:None)

    def test_section_and_selected_device_preferences_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ui.json';settings=UISettings(path)
            settings.save(180,devices_expanded=False,files_expanded=False,selected_device='ssh-example')
            restored=UISettings(path)
            self.assertFalse(restored.values['devices_expanded'])
            self.assertEqual(restored.values['selected_device'],'ssh-example')
            restored.reset()
            self.assertTrue(restored.values['files_expanded'])
            self.assertEqual(restored.values['selected_device'],'local')

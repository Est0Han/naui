"""Source Control acceptance on disposable repositories, no network/real SSH."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from naui.commands import local_command
from naui.plugins.git_service import GIT, GitService, GitChange, parse_status
from naui.plugins.base_git import Repository, GitPlugin
from naui.plugins.git_history import parse_log
from naui.plugin_api import DeviceInfo, PluginAPI
from naui.transfers import FileLocation
from naui.diff_model import parse_diff, side_rows, size_mode, AUTO_BYTES, HARD_BYTES
from naui.backends import LocalFileBackend, RemoteFileBackend
from test_workspace import RootedTransport


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'project'; self.root.mkdir()
        self.calls = []
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Naui Test')
        self.git('config', 'user.email', 'test@example.invalid')
        for name in ['modify.py', 'delete.py', 'staged.py', 'old õ.py', ':(glob)*.txt']:
            (self.root/name).write_text('original\n')
        self.git('add', '-A', '--', '.'); self.git('commit', '-qm', 'Initial')
        self.repo = Repository('local', str(self.root), status=self.status())
        self.messages, self.sections, self.opened, self.confirmations, self.timers = [], [], [], [], []
        self.devices = [DeviceInfo('local', 'local', 'LOCAL', 'connected', str(self.root), (str(self.root),), active=True)]
        self.backend = LocalFileBackend()
        def execute(*, device_id, cwd, command, callback):
            self.calls.append((device_id, cwd, command))
            try: result = local_command(command, cwd)
            except Exception as exc: callback(None, exc)
            else: callback(result, None)
        def read(location, cb):
            try: value = self.backend.read(location.path).content
            except Exception as error: cb(None, error)
            else: cb(value, None)
        def remove(location, revision, callback):
            try: self.backend.delete(location.path, revision)
            except Exception as error: callback(None, error)
            else: callback(None, None)
        self.api = SimpleNamespace(commands=SimpleNamespace(execute=execute),
            on_workspace_changed=lambda *args: None, add_command=lambda *args, **kw: None,
            devices=lambda: self.devices, selected_location=lambda: FileLocation('local', str(self.root)),
            message=self.messages.append, sidebar=lambda *args: self.sections.append(args[-1]),
            decorate_files=lambda *args: None, cancel_deferred=lambda *args: None,
            defer=lambda delay, cb: self.timers.append((delay, cb)),
            confirm=lambda *args: self.confirmations.append(args), open=lambda location: self.opened.append(location),
            read_text=read, exists=lambda loc, cb: cb(self.backend.transfer_properties(loc.path) is not None, None),
            properties=lambda loc, cb: cb(self.backend.properties(loc.path), None),
            files=SimpleNamespace(remove=remove, probe=lambda loc, cb: cb(self.backend.probe(loc.path).sample, None)),
            ui=SimpleNamespace(open_diff=lambda *args, **kwargs: self.opened.append((args, kwargs)), current_diff=lambda: None))
        self.service = GitService(self.api)
        self.plugin = GitPlugin(self.api)
        self.plugin.repositories[('local', str(self.root))] = self.repo

    def git(self, *args): return local_command((*GIT, *args), str(self.root))
    def status(self): return parse_status(self.git('status', '--porcelain=v2', '-z', '--branch', '--untracked-files=all'))
    def changes(self):
        self.repo.status = self.status()
        return {c.path: c for c in self.repo.status.changes}
    def op(self, action, **kwargs):
        self.repo.status = self.status(); result = []
        self.service.operation(self.repo, action, lambda data, error: result.append((data, error)), **kwargs)
        self.assertEqual(len(result), 1)
        if result[0][1]: raise result[0][1]
        return result[0][0]
    def patch(self, change, staged=False):
        result = []
        self.service.diff(self.repo, change, staged, lambda data, error: result.append((data, error)))
        self.assertIsNone(result[0][1]); return result[0][0].decode()

    def test_local_acceptance_groups_correct_diff_sides_and_unique_file_badge(self):
        (self.root/'modify.py').write_text('modified\n')
        (self.root/'delete.py').unlink()
        (self.root/'new test.py').write_text('new\n')
        (self.root/'staged.py').write_text('index\n'); self.git('add', '--', 'staged.py')
        (self.root/'staged.py').write_text('working\n')
        changes = self.changes()
        self.assertEqual({c.decoration for c in changes.values()}, {'M', 'D', '?'})
        self.assertEqual((changes['staged.py'].index, changes['staged.py'].worktree), ('M', 'M'))
        self.assertIn('+working', self.patch(changes['staged.py']))
        self.assertIn('-index', self.patch(changes['staged.py']))
        self.assertIn('+index', self.patch(changes['staged.py'], True))
        self.assertNotIn('working', self.patch(changes['staged.py'], True))
        deleted = parse_diff(self.patch(changes['delete.py']))
        self.assertEqual(deleted.documents[0].new_path, '/dev/null')
        self.assertTrue(all(line.new_number is None for line in deleted.hunks[0].lines))
        self.plugin.render()
        groups = {r['text']: r for r in next(r for r in self.sections[-1] if r.get('id') == 'selected-changes')['children']}
        self.assertIn('Changes  4', groups); self.assertIn('Staged Changes  1', groups)
        item = groups['Changes  4']['children'][0]
        self.assertTrue(callable(item['select']))
        self.assertIn('Discard Changes…', [name for name, cb in item['menu']])
        # Opening every changed file retrieves only its individual diff.
        for change in changes.values(): self.plugin.diff(self.repo, change)
        patches = [args[2] for args, options in self.opened]
        self.assertEqual(len(patches), 4); self.assertTrue(any('+new' in p for p in patches))
        self.assertTrue(all(options['location'].device_id == 'local' for args, options in self.opened))

    def test_stage_unstage_file_and_all_preserve_working_contents(self):
        (self.root/'modify.py').write_text('working\n')
        (self.root/'new.py').write_text('new\n')
        self.op('stage', change=self.changes()['modify.py'])
        self.assertEqual(self.changes()['modify.py'].index, 'M')
        self.op('unstage', change=self.changes()['modify.py'])
        self.assertEqual(self.changes()['modify.py'].index, '.')
        self.op('stage'); self.assertTrue(all(c.index != '?' for c in self.changes().values()))
        self.op('unstage'); self.assertEqual(self.changes()['new.py'].index, '?')
        self.assertEqual((self.root/'modify.py').read_text(), 'working\n')
        self.assertEqual((self.root/'new.py').read_text(), 'new\n')

    def test_rename_original_new_paths_and_literal_pathspecs(self):
        self.git('mv', '--', 'old õ.py', 'new õ.py')
        change = self.changes()['new õ.py']
        self.assertEqual((change.decoration, change.original), ('R', 'old õ.py'))
        self.assertIn('rename from', self.patch(change, True))
        self.op('unstage', change=change)
        self.assertEqual((self.root/'new õ.py').read_text(), 'original\n')
        name = ':(glob)*.txt'; (self.root/name).write_text('literal\n')
        (self.root/'modify.py').write_text('other\n')
        self.op('stage', change=self.changes()[name])
        self.assertEqual(self.changes()['modify.py'].index, '.')
        patch = self.patch(self.changes()[name], True)
        self.assertIn('+literal', patch); self.assertNotIn('other', patch)
        self.assertEqual(self.calls[-1][2][-2:], ('--', name))

    def test_discard_requires_confirmation_and_preserves_index(self):
        (self.root/'modify.py').write_text('staged\n'); self.git('add', '--', 'modify.py')
        (self.root/'modify.py').write_text('unstaged\n')
        change = self.changes()['modify.py']
        self.plugin.discard(self.repo, change)
        self.assertEqual((self.root/'modify.py').read_text(), 'unstaged\n')
        self.assertEqual(len(self.confirmations), 1)
        self.confirmations[0][-1]()
        self.assertEqual((self.root/'modify.py').read_text(), 'staged\n')
        self.assertEqual(self.changes()['modify.py'].index, 'M')

    def test_discard_untracked_cannot_recursively_delete_directory_or_newly_staged_file(self):
        file = self.root/'untracked'; file.write_text('keep\n')
        change = self.changes()['untracked']; self.plugin.discard(self.repo, change)
        self.git('add', '--', 'untracked'); self.confirmations[-1][-1]()
        self.assertTrue(file.exists()); self.assertIn('state changed', self.messages[-1])
        self.git('reset', '-q', 'HEAD', '--', 'untracked'); file.unlink(); file.mkdir(); (file/'child').write_text('keep')
        with self.assertRaises(ValueError): self.op('discard', change=change)
        self.assertEqual((file/'child').read_text(), 'keep')

    def test_tracked_discard_refuses_directory_replacement_and_rename_staging_is_scoped(self):
        file = self.root/'modify.py'; file.unlink(); file.mkdir(); (file/'keep').write_text('keep')
        change = self.changes()['modify.py']
        with self.assertRaises(ValueError): self.op('discard', change=change)
        self.assertEqual((file/'keep').read_text(), 'keep')
        self.git('mv', '--', 'old õ.py', 'new õ.py')
        (self.root/'new õ.py').write_text('edit rename\n')
        # The old path can reappear as an unrelated untracked file.
        (self.root/'old õ.py').write_text('unrelated\n')
        self.op('stage', change=self.changes()['new õ.py'])
        self.assertEqual(self.changes()['old õ.py'].index, '?')
        (self.root/'new õ.py').write_text('unstaged rename\n')
        self.op('discard', change=self.changes()['new õ.py'])
        self.assertEqual((self.root/'new õ.py').read_text(), 'edit rename\n')
        self.assertEqual((self.root/'old õ.py').read_text(), 'unrelated\n')

    def test_binary_untracked_and_committed_binary_have_no_text_payload(self):
        (self.root/'binary').write_bytes(b'\0\xffdata')
        self.plugin.diff(self.repo, self.changes()['binary'])
        self.assertIn('Binary files', self.opened[-1][0][2])
        self.git('add', '--', 'binary'); self.git('commit', '-qm', 'Binary fixture')
        (self.root/'binary').write_bytes(b'\0different')
        model = parse_diff(self.patch(self.changes()['binary']))
        self.assertTrue(model.documents[0].binary); self.assertFalse(model.hunks)

    def test_untracked_preview_limits_avoid_giant_reads_and_dense_patch_generation(self):
        from naui.diff_model import HARD_LINES
        file = self.root/'dense.py'; file.write_text('x\n' * (HARD_LINES + 1))
        self.plugin.diff(self.repo, self.changes()['dense.py'])
        self.assertIn('line limit', self.opened[-1][0][2])
        huge = self.root/'huge.py'
        with huge.open('wb') as stream: stream.truncate(8 * 1024 * 1024 + 1)
        self.api.read_text = lambda *args: self.fail('Large files must not be downloaded for diff preview')
        self.plugin.diff(self.repo, self.changes()['huge.py'])
        self.assertIn('8 MiB', self.opened[-1][0][2])

    def test_unborn_unstage_preserves_modified_and_new_files(self):
        self.root = self.base/'unborn'; self.root.mkdir(); self.git('init', '-q', '-b', 'main')
        file = self.root/'new.py'; file.write_text('index\n'); self.git('add', '--', 'new.py')
        file.write_text('working\n')
        self.repo.root = str(self.root)
        self.op('unstage', change=self.changes()['new.py'])
        self.assertEqual(file.read_text(), 'working\n'); self.assertEqual(self.changes()['new.py'].index, '?')
        self.op('stage'); self.op('unstage'); self.assertEqual(file.read_text(), 'working\n')

    def test_commit_validation_only_staged_and_failure_retains_draft(self):
        self.repo.draft = 'Keep this message'
        with self.assertRaises(ValueError): self.op('commit', message=' ')
        with self.assertRaises(ValueError): self.op('commit', message='No staged files')
        (self.root/'staged.py').write_text('index\n'); (self.root/'modify.py').write_text('working\n')
        self.op('stage', change=self.changes()['staged.py'])
        self.op('commit', message='Only staged')
        self.assertEqual(set(self.changes()), {'modify.py'})
        (self.root/'staged.py').write_text('next\n'); self.op('stage', change=self.changes()['staged.py'])
        hooks = self.root/'.git/hooks'; hook = hooks/'pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o700)
        self.plugin.mutate(self.repo, 'commit', message=self.repo.draft)
        self.assertEqual(self.repo.draft, 'Keep this message'); self.assertIsNone(self.repo.operation)
        self.assertIn('failed', self.messages[-1])

    def test_outgoing_incoming_push_and_fetch_use_local_bare_remote(self):
        bare = self.base/'remote.git'; bare.mkdir()
        local_command((*GIT, 'init', '--bare', '-q'), str(bare))
        self.git('remote', 'add', 'origin', str(bare)); self.git('push', '-qu', 'origin', 'main')
        (self.root/'staged.py').write_text('next\n'); self.op('stage'); self.op('commit', message='Outgoing fixture')
        self.repo.status = self.status()
        self.assertEqual((self.repo.status.ahead, self.repo.status.behind), (1, 0))
        self.assertEqual(self.repo.status.upstream, 'origin/main')
        result = []; self.service.outgoing(self.repo, lambda data, error: result.append(data))
        self.assertEqual(parse_log(result[0])[0].summary, 'Outgoing fixture')
        self.plugin.load_outgoing(self.repo); self.plugin.render()
        self.assertTrue(any(r['text'] == 'Outgoing commits  1' for r in next(r for r in self.sections[-1] if r.get('id') == 'selected-changes')['children']))
        self.op('push'); self.assertEqual(self.status().ahead, 0)
        other = self.base/'other'
        local_command((*GIT, 'clone', '-q', '--branch', 'main', str(bare), str(other)), str(self.base))
        for args in [('config', 'user.name', 'Naui Test'), ('config', 'user.email', 'test@example.invalid')]: local_command((*GIT, *args), str(other))
        (other/'incoming').write_text('incoming\n')
        for args in [('add', '--', 'incoming'), ('commit', '-qm', 'Incoming fixture'), ('push', '-q')]: local_command((*GIT, *args), str(other))
        self.op('fetch'); self.assertEqual(self.status().behind, 1)
        self.op('pull'); self.assertEqual(self.status().behind, 0)
        self.assertEqual((self.root/'incoming').read_text(), 'incoming\n')

    def test_real_merge_conflict_is_separate_and_discard_refused(self):
        self.git('checkout', '-qb', 'feature'); (self.root/'modify.py').write_text('feature\n')
        self.git('add', '--', 'modify.py'); self.git('commit', '-qm', 'Feature')
        self.git('checkout', '-q', 'main'); (self.root/'modify.py').write_text('main\n')
        self.git('add', '--', 'modify.py'); self.git('commit', '-qm', 'Main')
        with self.assertRaises(Exception): self.git('merge', '--no-edit', 'feature')
        change = self.changes()['modify.py']
        self.assertTrue(change.conflicted); self.assertEqual(change.decoration, '!')
        self.plugin.render()
        self.assertTrue(any(r['text'] == 'Conflicts  1' for r in next(r for r in self.sections[-1] if r.get('id') == 'selected-changes')['children']))
        self.plugin.discard(self.repo, change); self.assertFalse(self.confirmations)
        self.plugin.diff(self.repo, change); self.assertEqual(self.opened[-1], FileLocation('local', str(self.root/'modify.py')))

    def test_detached_head_clean_and_multiple_repository_device_identity(self):
        self.git('checkout', '--detach', '-q'); self.repo.status = self.status()
        self.plugin.render(); self.assertEqual('(detached)', self.sections[-1][1]['children'][0]['detail'])
        remote = DeviceInfo('ssh-test', 'ssh', 'example-server', 'connected', '/project', ('/project',))
        self.devices.append(remote)
        remote_repo = Repository('ssh-test', '/project', status=self.repo.status)
        self.plugin.repositories[('ssh-test', '/project')] = remote_repo
        self.plugin.render()
        self.assertEqual(len(next(r for r in self.sections[-1] if r.get('id') == 'repositories')['children']), 2)
        self.devices[-1] = DeviceInfo('ssh-test', 'ssh', 'example-server', 'disconnected', '/project', ('/project',))
        remote_repo.connected = False; self.plugin.render()
        self.assertEqual('Disconnected', next(r for r in self.sections[-1] if r.get('id') == 'repositories')['children'][1]['detail'])
        result = []; self.service.diff(remote_repo, GitChange('modify.py', '.', 'M'), False, lambda data, error: result.append(error))
        self.assertIn('disconnected', str(result[0]))

    def test_remote_commands_use_remote_cwd_and_transport_not_mount(self):
        backend = RemoteFileBackend(RootedTransport('example-server', self.base)); self.addCleanup(backend.close)
        repo = Repository('ssh-test', '/project', status=self.status())
        commands = []
        def execute(*, device_id, cwd, command, callback):
            commands.append((device_id, cwd, command))
            try: data = backend.run_command(command, cwd)
            except Exception as exc: callback(None, exc)
            else: callback(data, None)
        service = GitService(SimpleNamespace(commands=SimpleNamespace(execute=execute)))
        (self.root/'modify.py').write_text('remote\n')
        result = []; service.status(repo, lambda data, error: result.append(parse_status(data)))
        change = result[0].changes[0]
        service.operation(repo, 'stage', lambda data, error: self.assertIsNone(error), change=change)
        service.diff(repo, change, True, lambda data, error: self.assertIn(b'+remote', data))
        service.operation(repo, 'unstage', lambda data, error: self.assertIsNone(error), change=change)
        self.assertTrue(all(device == 'ssh-test' and cwd == '/project' for device, cwd, argv in commands))
        self.assertFalse(any('mounts' in arg for device, cwd, argv in commands for arg in argv))

    def test_nested_repository_directory_does_not_break_outer_status(self):
        nested = self.root/'nested'; nested.mkdir()
        local_command((*GIT, 'init', '-q', '-b', 'main'), str(nested))
        (nested/'file.py').write_text('nested\n')
        self.assertTrue(self.changes()['nested'].directory)
        self.plugin.render(); self.assertIsNone(self.repo.error)
        change = self.changes()['nested']
        self.plugin.discard(self.repo, change)
        self.assertFalse(self.confirmations); self.assertTrue((nested/'file.py').exists())

    def test_staged_delete_plus_recreated_file_has_two_states_and_one_badge_file(self):
        self.git('rm', '-q', '--', 'delete.py'); (self.root/'delete.py').write_text('recreated\n')
        self.repo.status = self.status()
        states = [c for c in self.repo.status.changes if c.path == 'delete.py']
        self.assertEqual(len(states), 2)
        new = next(c for c in states if c.index == '?')
        self.api.ui.current_diff = lambda: {'device_id': 'local', 'repository': str(self.root),
            'location': FileLocation('local', str(self.root/'delete.py')), 'staged': False}
        self.assertEqual(self.plugin.current_change()[1], new)
        self.assertTrue(self.plugin.action_enabled('stage'))
        self.plugin.current_action('stage'); self.repo.status = self.status()
        self.assertEqual(len(self.repo.status.changes), 1)
        self.assertEqual(self.repo.status.changes[0].index, 'M')

    def test_refresh_debounce_and_no_auto_network_fetch(self):
        self.plugin.refresh(self.repo); self.plugin.refresh(self.repo)
        self.assertTrue(all('fetch' not in argv and 'push' not in argv and 'pull' not in argv for _, _, argv in self.calls))
        for _ in range(30): self.plugin.changed(SimpleNamespace(device_id='local', path=str(self.root/'modify.py')))
        self.assertTrue(all(delay == 500 for delay, cb in self.timers))

    def test_current_diff_commands_use_exact_device_repository_and_enabled_state(self):
        (self.root/'modify.py').write_text('working\n')
        change = self.changes()['modify.py']
        context = {'device_id': 'local', 'repository': str(self.root),
                   'location': FileLocation('local', str(self.root/'modify.py')), 'staged': False}
        self.api.ui.current_diff = lambda: context
        self.assertTrue(self.plugin.action_enabled('stage'))
        self.assertFalse(self.plugin.action_enabled('unstage'))
        self.plugin.current_action('stage')
        self.changes(); self.assertTrue(self.plugin.action_enabled('unstage'))
        self.assertFalse(self.plugin.action_enabled('stage'))
        context['device_id'] = 'ssh-test'
        self.assertIsNone(self.plugin.current_change())
        context['device_id'] = 'local'; context['historical'] = True
        self.assertIsNone(self.plugin.current_change())

    def test_mutation_is_serialized_and_stop_is_idempotent_with_late_callbacks(self):
        (self.root/'modify.py').write_text('working\n'); change = self.changes()['modify.py']
        pending = []
        self.api.commands.execute = lambda **kw: pending.append(kw['callback'])
        self.plugin.mutate(self.repo, 'stage', change)
        self.assertEqual(self.repo.operation, 'stage')
        self.plugin.mutate(self.repo, 'unstage', change)
        self.assertEqual(len(pending), 1); self.assertIn('busy', self.messages[-1])
        data = self.git('status', '--porcelain=v2', '-z', '--branch', '--untracked-files=all')
        self.plugin.stop(); self.plugin.stop(); pending[0](data, None)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.status().changes[0].index, '.')


class DiffTests(unittest.TestCase):
    def test_hunks_line_numbers_alignment_newline_metadata_and_multiple_files(self):
        patch = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -2,2 +2,3 @@ block\n-old\n+new\n+extra\n same\n\\ No newline at end of file\n'
        model = parse_diff(patch + 'diff --git a/y b/y\n--- /dev/null\n+++ b/y\n@@ -0,0 +1 @@\n+created\n')
        self.assertEqual(len(model.documents), 2)
        h = model.hunks[0]
        self.assertEqual((h.old_start, h.old_count, h.new_start, h.new_count), (2, 2, 2, 3))
        rows = side_rows(h)
        self.assertEqual((rows[0][0].old_number, rows[0][1].new_number), (2, 2))
        self.assertIsNone(rows[1][0]); self.assertEqual(rows[1][1].text, 'extra')
        self.assertEqual(h.lines[-1].kind, 'note')
        self.assertEqual(model.hunks[-1].lines[0].new_number, 1)

    def test_deleted_added_zero_ranges_and_header_like_content(self):
        patch = '--- a/x\n+++ b/x\n@@ -1 +1 @@\n---- looks like header\n++++ also content\n'
        model = parse_diff(patch)
        self.assertEqual(model.hunks[0].lines[0].text, '--- looks like header')
        self.assertEqual(model.hunks[0].lines[1].text, '+++ also content')
        self.assertEqual(len(model.documents), 1)
        with self.assertRaises(ValueError): parse_diff('@@ -1,2 +1 @@\n-a\n+b\n')

    def test_binary_rename_and_size_limits(self):
        model = parse_diff('diff --git a/a b/b\nsimilarity index 100%\nrename from a\nrename to b\nBinary files a/a and b/b differ\n')
        self.assertTrue(model.documents[0].binary)
        self.assertIn('rename to b', model.documents[0].metadata)
        self.assertEqual(size_mode('x' * (AUTO_BYTES+1)), 'large')
        self.assertEqual(size_mode('x' * (HARD_BYTES+1)), 'blocked')
        with self.assertRaises(ValueError): parse_diff('x' * (HARD_BYTES+1))

    def test_theme_styles_and_native_buffer_line_numbers_without_display(self):
        import gi
        gi.require_version('Gtk', '4.0'); gi.require_version('GtkSource', '5')
        from gi.repository import GtkSource
        from naui.diff_view import DiffView
        buffer = GtkSource.Buffer()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme('Adwaita')
        buffer.set_style_scheme(scheme)
        DiffView.fill(buffer, [('12  deleted', 'removed'), ('13  added', 'added')])
        self.assertIn('13', buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))
        tag = buffer.get_tag_table().lookup('diff-added')
        self.assertIsNotNone(tag); self.assertTrue(tag in buffer.get_iter_at_line(1)[1].get_tags())

    def test_sidebar_nested_callbacks_guarded_and_boundaries_checked(self):
        calls = []
        host = SimpleNamespace(_shutting_down=False, plugins=SimpleNamespace(fail=lambda *args: calls.append('failed')))
        api = PluginAPI(host, 'example-plugin', {'views'})
        rows = api._rows([{'text': 'Repo', 'children': [{'text': 'File', 'select': lambda: calls.append('select'),
            'menu': [('Stage', lambda: calls.append('stage'))], 'input': {'changed': lambda text: calls.append(text)}}]}])
        child = rows[0]['children'][0]
        child['select'](); child['menu'][0][1](); child['input']['changed']('draft')
        self.assertEqual(calls, ['select', 'stage', 'draft'])
        api.quiescing = True; child['select'](); self.assertEqual(len(calls), 3)
        with self.assertRaises(ValueError): api._rows([{'text': 'x'}] * 2001)

    def test_ignored_and_copy_status_codes(self):
        status = parse_status(b'! ignored/\0' + b'2 C. N... 100644 100644 100644 a b C100 target\0source\0')
        self.assertEqual([c.decoration for c in status.changes], ['!', 'C'])
        self.assertFalse(status.changes[0].conflicted)

class HostBoundaryTests(unittest.TestCase):
    def test_file_removal_permission_revision_and_directory_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = LocalFileBackend(); root = Path(directory); file = root/'file'; file.write_text('keep')
            calls = []
            device = SimpleNamespace(type='local', filesystem_backend=backend)
            host = SimpleNamespace(_shutting_down=False, workspace=SimpleNamespace(devices={'local': device}),
                check_mutation=lambda *args: None, plugins=SimpleNamespace(fail=lambda *args: calls.append(args)))
            def work(job, callback):
                try: result = job()
                except Exception as error: callback(None, error)
                else: callback(result, None)
            host.work = work
            api = PluginAPI(host, 'example-plugin', set())
            location = FileLocation('local', str(file)); revision = backend.properties(str(file)).revision
            with self.assertRaises(PermissionError): api.files.remove(location, revision, lambda *args: None)
            api.permissions = frozenset({'filesystem_write'})
            file.write_text('changed')
            api.files.remove(location, revision, lambda result, error: calls.append(error))
            self.assertIsNotNone(calls[-1]); self.assertEqual(file.read_text(), 'changed')
            folder = root/'folder'; folder.mkdir(); (folder/'child').write_text('keep')
            api.files.remove(FileLocation('local', str(folder)), backend.properties(str(folder)).revision, lambda result, error: calls.append(error))
            self.assertIsInstance(calls[-1], ValueError); self.assertTrue((folder/'child').exists())
            api.files.remove(location, backend.properties(str(file)).revision, lambda result, error: calls.append(error))
            self.assertIsNone(calls[-1]); self.assertFalse(file.exists())

    def test_missing_newlines_and_ai_review_share_patch_model(self):
        from naui.diff_model import text_diff
        from naui.ai_agents import plugin_snapshot, review_changes
        model = parse_diff(text_diff('old', 'new', 'old/x', 'new/x'))
        self.assertEqual([l.kind for l in model.hunks[0].lines], ['removed', 'note', 'added', 'note'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'plugin.py').write_text('old')
            before = plugin_snapshot(root); (root/'plugin.py').write_text('new')
            model = parse_diff(review_changes(root, before))
            self.assertEqual(model.documents[0].new_path, 'after/plugin.py')
            self.assertEqual(model.hunks[0].lines[2].text, 'new')

    def test_diff_permission_device_identity_and_historical_context(self):
        calls = []; local = SimpleNamespace(type='local'); remote = SimpleNamespace(type='ssh')
        host = SimpleNamespace(_shutting_down=False, workspace=SimpleNamespace(devices={'local': local, 'ssh-test': remote}),
            open_diff_view=lambda *args, **kwargs: calls.append((args, kwargs)))
        api = PluginAPI(host, 'example-plugin', set())
        with self.assertRaises(PermissionError): api.ui.open_diff('local', 'Diff', '')
        api.permissions = frozenset({'views', 'filesystem_read', 'editor_navigation'})
        with self.assertRaises(PermissionError): api.ui.open_diff('ssh-test', 'Diff', '')
        api.permissions = api.permissions | {'remote_access'}
        with self.assertRaises(ValueError): api.ui.open_diff('local', 'Diff', '', location=FileLocation('ssh-test', '/project/x'))
        api.ui.open_diff('local', 'Diff', '', location=FileLocation('local', '/project/x'), repository='/project', staged=True)
        context = calls[-1][1]['context']; self.assertEqual(context['repository'], '/project'); self.assertTrue(context['staged'])

    def test_diff_modes_render_numbers_and_navigation_without_native_window(self):
        import gi
        gi.require_version('Gtk', '4.0'); gi.require_version('GtkSource', '5')
        from gi.repository import GtkSource
        from naui.diff_view import DiffView
        label = SimpleNamespace(set_text=lambda text: None, set_visible=lambda value: None)
        view = SimpleNamespace(model=parse_diff('--- a/x\n+++ b/x\n@@ -7 +7,2 @@\n-old\n+new\n+extra\n'),
            _shutting_down=False, mode=SimpleNamespace(get_selected=lambda: 0), left_box=label, right_title=label,
            diff_context={'staged': True}, hunk_offsets=[], current_hunk=-1, left_buffer=GtkSource.Buffer(),
            buffer=GtkSource.Buffer(), fill=DiffView.fill, patch='')
        DiffView.render(view)
        text = view.buffer.get_text(view.buffer.get_start_iter(), view.buffer.get_end_iter(), True)
        self.assertIn('7 ~+ new', text); self.assertIn('8  + extra', text)
        before = view.left_buffer.get_text(view.left_buffer.get_start_iter(), view.left_buffer.get_end_iter(), True)
        self.assertIn('7 ~- old', before)
        self.assertEqual(view.buffer.get_line_count(), view.left_buffer.get_line_count())
        for kind in ('modified', 'added', 'removed'):
            tag = view.buffer.get_tag_table().lookup('diff-' + kind)
            self.assertTrue(tag.get_property('paragraph-background-set'))
        view.mode.get_selected = lambda: 1; DiffView.render(view)
        text = view.buffer.get_text(view.buffer.get_start_iter(), view.buffer.get_end_iter(), True)
        self.assertIn('- old', text); self.assertIn('+ new', text)
        heading_tags = {tag.get_property('name') for tag in view.buffer.get_start_iter().get_tags()}
        self.assertEqual(heading_tags, {'diff-heading'}, 'Old row styles leaked into the unified header')
        positions = []; view.view = SimpleNamespace(scroll_to_iter=lambda iterator, *args: positions.append(iterator.get_line()))
        DiffView.change(view, 1); self.assertEqual(positions[-1], view.hunk_offsets[0])
        view.model = parse_diff('--- a/x\n+++ b/x\n@@ -1,2 +1 @@\n-deleted\n unchanged\n')
        view.mode.get_selected = lambda: 0
        DiffView.render(view)
        before = view.left_buffer.get_text(view.left_buffer.get_start_iter(), view.left_buffer.get_end_iter(), True)
        after = view.buffer.get_text(view.buffer.get_start_iter(), view.buffer.get_end_iter(), True)
        self.assertIn('1  - deleted', before)
        self.assertNotIn('deleted', after)
        self.assertEqual(view.buffer.get_line_count(), view.left_buffer.get_line_count())

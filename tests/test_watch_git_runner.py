"""Real native file events, PTY runs, disposable Git and simulated SSH I/O."""
from dataclasses import replace
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from naui.backends import LocalFileBackend, RemoteFileBackend, FileSnapshot, FileError
from naui.commands import CommandService, local_command
from naui.external_changes import ExternalChange
from naui.plugins.base_runner import file_command, runnable, Runner
from naui.plugins.base_git import parse_status, repository_path, GitPlugin, GIT, Repository
from naui.runner_commands import terminal_command
from naui.transfers import FileLocation
from naui.ui_settings import UISettings
from naui.workspace import Workspace
from test_workspace import RootedTransport, wait_for
from test_shell_cwd import ShellHarness
from naui.navigation import PREPARE_RUN
from naui.file_watch import FileWatchService, FileChangeEvent, snapshot_changes, directory_snapshot, parse_watch_snapshot
from naui.lifecycle import Sources, FileWork
from naui.gui import EditorPane, NauiWindow
from gi.repository import GtkSource, GLib


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(str(self.root))
        self.sources = Sources()
        self.watch = FileWatchService(self.sources, self.workspace.devices.__getitem__, remote_min=.02, remote_max=.05)
        self.events = []
        self.addCleanup(self.finish)

    def finish(self):
        self.watch.close()
        for device in self.workspace.devices.values():
            if device.type == 'ssh': device.filesystem_backend.close()
        self.watch.join(); self.sources.close()

    def test_native_local_create_modify_rename_delete_and_unsubscribe(self):
        handle = self.watch.watch(FileLocation('local', str(self.root)), self.events.append)
        self.assertIsNotNone(self.watch.states[('local',str(self.root))].monitor)
        path = self.root / 'unic õ.txt'; path.write_text('old')
        wait_for(lambda: any(event.event == 'created' and event.path == str(path) for event in self.events))
        path.write_text('external update')
        wait_for(lambda: any(event.event == 'modified' and event.path == str(path) for event in self.events))
        target = self.root/'renamed.txt'; path.rename(target)
        wait_for(lambda: any(event.event == 'moved' and event.old_path == str(path) and event.path == str(target) for event in self.events))
        target.unlink()
        wait_for(lambda: any(event.event == 'deleted' and event.path == str(target) for event in self.events))
        handle.close(); handle.close()
        self.assertFalse(self.watch.states)
        self.assertFalse(self.watch.callbacks)

    def test_shared_monitor_and_shutdown_releases_sources_callbacks(self):
        location = FileLocation('local', str(self.root))
        a = self.watch.watch(location, self.events.append)
        b = self.watch.watch(location, self.events.append)
        self.assertEqual(len(self.watch.states), 1)
        a.close(); self.assertEqual(len(self.watch.states), 1)
        self.watch.close(); self.watch.close()
        self.assertFalse(self.watch.states); self.assertFalse(self.watch.callbacks)
        with self.assertRaises(RuntimeError): self.watch.watch(location, self.events.append)
        b.close()

    def test_symlinks_and_recursive_watches_refused(self):
        target = self.root/'target'; target.mkdir()
        link = self.root/'link'; link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError): self.watch.watch(FileLocation('local',str(link)), self.events.append)
        with self.assertRaises(ValueError): self.watch.watch(FileLocation('local',str(target)), self.events.append, recursive=True)
        self.assertFalse(self.watch.states)

    def test_snapshot_rename_requires_unique_inode_and_handles_metadata(self):
        path = self.root/'old'; path.write_text('value')
        backend = LocalFileBackend()
        before = directory_snapshot(backend, str(self.root))
        new = self.root/'new'; path.rename(new)
        after = directory_snapshot(backend, str(self.root))
        events = snapshot_changes('ssh-a', before, after)
        self.assertEqual([(event.event,event.old_path,event.path) for event in events], [('moved',str(path),str(new))])
        new.chmod(0o700)
        self.assertEqual(snapshot_changes('ssh-a', after, directory_snapshot(backend,str(self.root)))[0].event, 'metadata')
        self.assertTrue(all(event.device_id == 'ssh-a' for event in events))

    def test_remote_polling_external_create_rename_delete_and_disconnect(self):
        device = self.workspace.add_ssh('server-a')
        folder = self.root/'project'; folder.mkdir()
        device.filesystem_backend.connect(RootedTransport('server-a',self.root))
        device.state = 'connected'
        # Force fallback: optional remote inotify is independently covered below.
        device.filesystem_backend.watch_hint = lambda path: None
        handle = self.watch.watch(FileLocation(device.id,'/project'), self.events.append)
        wait_for(lambda: self.watch.states[(device.id,'/project')].snapshot is not None)
        path = folder/'remote õ.txt'; path.write_text('remote')
        wait_for(lambda: any(event.event == 'created' and event.path == '/project/remote õ.txt' for event in self.events))
        path.rename(folder/'renamed.txt')
        wait_for(lambda: any(event.event == 'moved' and event.old_path == '/project/remote õ.txt' for event in self.events))
        (folder/'renamed.txt').unlink()
        wait_for(lambda: any(event.event == 'deleted' for event in self.events))
        device.filesystem_backend.close(); device.state = 'disconnected'
        self.watch.tick()
        self.assertIsNone(self.watch.states[(device.id,'/project')].snapshot)
        handle.close()

    def test_remote_snapshot_missing_parent_directory_and_symlink_behavior(self):
        folder=self.root/'project';folder.mkdir();file=folder/'file.txt';file.write_text('data')
        backend=RemoteFileBackend(RootedTransport('server-a',self.root))
        try:
            before=backend.watch_snapshot('/project')
            file.unlink();folder.rmdir()
            self.assertEqual(snapshot_changes('server-a',before,backend.watch_snapshot('/project'))[0].event,'deleted')
            target=self.root/'target';target.mkdir();folder.symlink_to(target,target_is_directory=True)
            with self.assertRaises(FileError):backend.watch_snapshot('/project')
        finally:backend.close()

    def test_remote_snapshot_parser_validates_nul_records_and_metadata(self):
        # Use joins so NUL followed by a digit is not an octal Python escape.
        record='\0'.join(['õ\nfile','f','5','640','123.4567890000','1000:1000','1:2:640:5:123.4567890000:123.5:1000:1000','']).encode()
        snapshot=parse_watch_snapshot(record,'/project')
        self.assertEqual(snapshot['/project/õ\nfile'].mode,0o640)
        self.assertEqual(snapshot['/project/õ\nfile'].mtime,123)
        for data in (b'bad',record.replace('õ\nfile'.encode(),b'../bad'),record.replace(b'\x00f\x00',b'\x00unknown\x00')):
            with self.assertRaises(ValueError):parse_watch_snapshot(data,'/project')

    def test_remote_hint_parent_reaps_its_child_on_stdin_eof(self):
        from naui.remote_watch import WATCH_SCRIPT
        helpers = self.root/'bin'; helpers.mkdir()
        pid = self.root/'watch-pid'
        tool = helpers/'inotifywait'
        tool.write_text('#!/bin/sh\necho $$ > "$NAUI_TEST_PID"\nprintf "naui-event\\n"\nexec sleep 30\n')
        tool.chmod(0o700)
        environment = dict(os.environ, PATH=str(helpers)+os.pathsep+os.environ['PATH'], NAUI_TEST_PID=str(pid))
        process = subprocess.Popen(['bash','-c',WATCH_SCRIPT,'naui',str(self.root)],stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment)
        try:
            wait_for(pid.exists)
            child = int(pid.read_text())
            process.stdin.close()
            process.wait(timeout=3)
            self.assertEqual(process.returncode,0)
            with self.assertRaises(ProcessLookupError): os.kill(child,0)
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stdout.close(); process.stderr.close()


class RunnerTests(unittest.TestCase):
    def test_mapping_and_executable_permission(self):
        self.assertEqual(file_command('/project/main.py',0o600,['--debug']),('python3','--','/project/main.py','--debug'))
        self.assertEqual(file_command('/x.sh',0o600),('bash','--','/x.sh'))
        self.assertEqual(file_command('/x.js',0o600),('node','--','/x.js'))
        self.assertEqual(file_command('/tool',0o700),('/tool',))
        with self.assertRaises(ValueError): file_command('/notes.txt',0o600)

    def test_shell_quoting_does_not_execute_filename_or_argument_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            data = 'space õ\nquote\' $(touch UNAUTHORIZED); *'
            command = terminal_command(directory,('printf','%s',data))
            self.assertEqual(command.count('\n'),1)
            result = subprocess.run(['bash','-c',command],capture_output=True,check=True)
            self.assertEqual(result.stdout.decode(),data)
            self.assertFalse((path/'UNAUTHORIZED').exists())

    def test_missing_interpreter_message_is_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['bash','-c',terminal_command(directory,('naui-no-such-interpreter','--','file'))],capture_output=True)
            self.assertIn(b'not found',result.stderr)
            self.assertEqual(result.returncode,127)

    def test_runner_retains_origin_device_even_when_selected_tree_differs(self):
        calls=[]
        api=SimpleNamespace(add_file_action=lambda *args:None,add_shortcut=lambda *args:None,add_command=lambda *args,**kwargs:None,
            terminals=SimpleNamespace(run=lambda **kwargs:calls.append(kwargs) or 'run-1', interrupt=calls.append),
            message=lambda message:self.fail(message))
        runner=Runner(api)
        entry=SimpleNamespace(mode=0o600)
        runner.run(FileLocation('raspberrypi','/home/pi/test.py'),entry)
        self.assertEqual(calls[0]['device_id'],'raspberrypi')
        self.assertEqual(calls[0]['cwd'],'/home/pi')
        runner.stop_run(); self.assertEqual(calls[1],'run-1')

    def test_current_python_document_requests_its_remote_terminal_without_local_stat(self):
        calls=[]
        document=SimpleNamespace(location=FileLocation('raspberrypi','/home/pi/main.py'),dirty=False)
        api=SimpleNamespace(add_file_action=lambda *args:None,add_shortcut=lambda *args:None,add_command=lambda *args,**kwargs:None,
            terminals=SimpleNamespace(run=lambda **kwargs:calls.append(kwargs) or 'run'), current_document=lambda:document,
            properties=lambda *args:self.fail('Required stat on a disconnected device before reconnect'),
            message=lambda message:self.fail(message))
        Runner(api).current()
        self.assertEqual(calls[0]['device_id'],'raspberrypi')

    def test_real_local_and_remote_bash_pty_run_readiness_and_private_history(self):
        for remote in (False,True):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                file=root/'run õ.py'; file.write_text('import sys; print("NAUI_RUN_OK", sys.argv[1])\n')
                harness=ShellHarness(root,remote=remote)
                try:
                    harness.command('history -c; HISTFILE='+shlex.quote(str(root/'private-history'))+'\n')
                    harness.command('echo NAUI_NORMAL_HISTORY_SENTINEL\n')
                    previous=len(harness.events)
                    harness.send(PREPARE_RUN)
                    harness.wait(lambda:any(event['action']=='navigation-ready' for event in harness.events[previous:]))
                    count=len(harness.cwds)
                    harness.send(terminal_command(directory,file_command(str(file),0o600,['ARG_SECRET_TEST'])).encode())
                    harness.wait(lambda:len(harness.cwds)>count)
                    harness.wait(lambda:b'NAUI_RUN_OK ARG_SECRET_TEST\r\n' in harness.output)
                    history=root/'history'
                    harness.command('history -w '+shlex.quote(str(history))+'\n')
                    self.assertFalse('ARG_SECRET_TEST' in history.read_text(), 'Runner command leaked into shell history')
                    self.assertTrue('NAUI_NORMAL_HISTORY_SENTINEL' in history.read_text(), 'Ordinary shell history was removed')
                    self.assertEqual(harness.cwds[-1]['location'],'remote' if remote else 'local')
                finally:harness.close()

    def test_core_run_waits_for_remote_prompt_before_pty_input(self):
        surface=SimpleNamespace(_shutting_down=False, runtime=SimpleNamespace(id='remote-terminal',device_id='ssh-a',closed=False),
            pending_runs={'remote-terminal':{'id':'run','device_id':'ssh-a','cwd':'/project','text':'python3 -- main.py\n'}},
            workspace=SimpleNamespace(devices={'ssh-a':SimpleNamespace(connected=True)}),at_prompt=False,
            navigation_pending=None,active_session='ssh-session',contexts={'ssh-session':{'path':'/tmp'}},
            terminal=SimpleNamespace(send_input=lambda data:self.fail('Sent into busy remote program')))
        NauiWindow.prepare_run(surface)
        self.assertIsNone(surface.navigation_pending)

    def test_core_remote_run_handshake_keeps_origin_when_local_tree_selected(self):
        from test_sync_controller import ControllerSurface
        window=ControllerSurface()
        window.pending_runs,window.running_runs={},{}
        for method in ('queue_run','prepare_run','interrupt_run'):
            setattr(window,method,getattr(NauiWindow,method).__get__(window))
        try:
            window.local('session-start',host='test-server',options=[],session='remote-run')
            window.on_message({'action':'session-ready','location':'remote','session':'remote-run'})
            window.on_message({'action':'cwd','location':'remote','session':'remote-run','path':'/project','home':'/home/test'})
            device=window.workspace.devices[window.runtime.device_id]
            window.workspace.active_device_id='local'
            identity=window.queue_run(device.id,'/project',('python3','--','/project/main.py'),False)
            self.assertEqual(window.input[-1],PREPARE_RUN)
            self.assertNotIn(b'python3',window.input)
            window.on_message({'action':'navigation-ready','location':'remote','session':'remote-run','path':'/project','home':'/home/test'})
            self.assertEqual(window.input[-1],terminal_command('/project',('python3','--','/project/main.py')).encode())
            self.assertEqual(window.running_runs[identity][1],device.id)
            window.interrupt_run(identity);self.assertEqual(window.input[-1],b'\x03')
            window.on_message({'action':'cwd','location':'remote','session':'remote-run','path':'/project','home':'/home/test'})
            count=len(window.input);window.interrupt_run(identity);self.assertEqual(len(window.input),count)
            self.assertFalse(window._shutting_down)
        finally:
            window.authentication.close();window.authentication.cleanup()
            for device in window.workspace.devices.values():
                if device.type=='ssh':device.filesystem_backend.close()


class GitTests(unittest.TestCase):
    def test_porcelain_records_rename_untracked_unicode_and_branch(self):
        data=(b'# branch.head main\0# branch.ab +2 -1\0'
              b'1 .M N... 100644 100644 100644 abc def space name.py\0'
              b'2 R. N... 100644 100644 100644 abc def R100 new name.py\0old name.py\0'
              + '? õ\nquote\' file.py\0'.encode())
        status=parse_status(data)
        self.assertEqual((status.branch,status.ahead,status.behind),('main',2,1))
        self.assertEqual([change.decoration for change in status.changes],['M','R','?'])
        self.assertEqual(status.changes[1].original,'old name.py')
        self.assertEqual(status.changes[2].path,"õ\nquote' file.py")

    def test_untrusted_git_paths_and_malformed_records_rejected(self):
        for record in (b'? ../outside\0',b'? /absolute\0',b'2 R.\0',b'1 blah\0'):
            with self.subTest(record=record),self.assertRaises(ValueError):parse_status(record)

    def test_repository_paths_preserve_newline_and_reject_relative(self):
        self.assertEqual(repository_path(b'/project\nname\n'),'/project\nname')
        with self.assertRaises(ValueError):repository_path(b'../outside\n')

    def test_real_git_detection_status_staged_rename_and_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def git(*argv):return local_command((*GIT,*argv),directory)
            git('init','-q');git('config','user.name','Naui Test');git('config','user.email','naui-test@example.invalid')
            file=root/'old õ.py';file.write_text('print("before")\n')
            git('add','--',file.name);git('commit','-qm','Initial test fixture')
            git('mv','--',file.name,'new õ.py')
            (root/'new õ.py').write_text('print("after")\n')
            (root/'untracked file.txt').write_text('untracked')
            self.assertEqual(repository_path(git('rev-parse','--show-toplevel')),str(root))
            status=parse_status(git('status','--porcelain=v2','-z','--branch','--untracked-files=all'))
            renamed=next(change for change in status.changes if change.path=='new õ.py')
            self.assertEqual((renamed.index,renamed.worktree,renamed.original),('R','M','old õ.py'))
            diff=git('diff','--no-ext-diff','--no-textconv','--','new õ.py').decode()
            self.assertIn('+print("after")',diff)
            self.assertIn('rename from',git('diff','--cached','--no-ext-diff','--no-textconv').decode())

    def test_git_runs_on_simulated_remote_device_without_repository_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'project').mkdir()
            subprocess.run(['git','init','-q',str(root/'project')],check=True)
            (root/'project/remote file.py').write_text('remote=1')
            backend=RemoteFileBackend(RootedTransport('server-a',root))
            try:
                status=parse_status(backend.run_command((*GIT,'status','--porcelain=v2','-z','--branch','--untracked-files=all'),'/project'))
                self.assertEqual(status.changes[0].path,'remote file.py')
            finally:backend.close()

    def test_refresh_debounces_and_separates_devices(self):
        scheduled,cancelled=[],[]
        api=SimpleNamespace(on_workspace_changed=lambda callback:None,add_command=lambda *args,**kwargs:None,
            defer=lambda delay,callback:scheduled.append((delay,callback)) or len(scheduled),cancel_deferred=cancelled.append)
        plugin=GitPlugin(api)
        a=Repository('ssh-a','/project');b=Repository('ssh-b','/project')
        plugin.repositories={('ssh-a','/project'):a,('ssh-b','/project'):b}
        for _ in range(20):plugin.changed(SimpleNamespace(device_id='ssh-a',path='/project/main.py'))
        self.assertEqual(len(cancelled),19)
        self.assertIsNone(b.timer)
        self.assertEqual(scheduled[-1][0],500)
        plugin.stop()


class CommandAndSettingsTests(unittest.TestCase):
    def test_local_command_output_bounded_and_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileError):local_command(['python3','-c','print("x"*100000)'],directory,limit=1024)
            stop=threading.Event();stop.set()
            with self.assertRaises(FileError):local_command(['sleep','30'],directory,cancelled=stop)
            service=CommandService();service.close();service.close()
            with self.assertRaises(FileError):service.execute(LocalFileBackend(),['true'],directory)

    def test_sidebar_settings_roundtrip_bounds_and_invalid_config_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ui.json';settings=UISettings(path)
            settings.save(360);self.assertEqual(UISettings(path).width,360)
            settings.save(900);self.assertEqual(UISettings(path).width,360)
            path.write_text('invalid');UISettings(path).save(320)
            self.assertEqual(path.read_text(),'invalid')


class Control:
    def __init__(self): self.text, self.visible, self.sensitive = '', False, True
    def set_text(self, value): self.text = value
    def set_visible(self, value): self.visible = value
    def set_sensitive(self, value): self.sensitive = value


class ExternalEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'config.txt';self.path.write_text('original\n')
        self.backend=LocalFileBackend();self.sources=Sources();self.work=FileWork(self.sources)
        self.workspace=Workspace(str(self.root))
        self.watch=FileWatchService(self.sources,self.workspace.devices.__getitem__)
        self.views,self.confirmations,self.moved=[],[],[]
        self.window=SimpleNamespace(_shutting_down=False,sources=self.sources,work=self.work.submit,file_watch=self.watch,
            code_navigation=SimpleNamespace(loaded=lambda editor:None,relocated=lambda editor,old:self.moved.append((old,editor.path))),
            current_editor=lambda:None,open_text_view=lambda *args:self.views.append(args),
            confirm_discard=lambda message,callback:self.confirmations.append(callback),
            confirm_action=lambda title,message,button,callback:self.confirmations.append(callback))
        buffer=GtkSource.Buffer();buffer.set_text('original\n');buffer.set_modified(False)
        self.editor=SimpleNamespace(_shutting_down=False,window=self.window,backend=self.backend,path=str(self.path),device_id='local',
            revision=self.backend.read(str(self.path)).revision,buffer=buffer,external=ExternalChange(),loading=False,
            saving=False,disconnected=False,watch_handle=None,watch_timer=None,watch_busy=False,watch_again=False,
            status=Control(),external_controls=Control(),keep_button=Control(),update_label=lambda:None,
            view=SimpleNamespace(set_editable=lambda value:None))
        # Match the real buffer's dirty flag dynamically rather than caching it.
        class Surface:
            path = EditorPane.path
            revision = EditorPane.revision
            external = EditorPane.external
            @property
            def dirty(self):return self.buffer.get_modified()
        surface=Surface();surface.__dict__.update(self.editor.__dict__);self.editor=surface
        from naui.editor_model import EditorDocument
        from naui.editor_view import GtkBufferAdapter
        surface.document=EditorDocument('local',str(self.path),self.backend,GtkBufferAdapter(buffer),self.backend.read(str(self.path)).revision)
        surface.editing=SimpleNamespace(loaded=lambda text:None,configure=lambda:None)
        self.editor.text=lambda:buffer.get_text(buffer.get_start_iter(),buffer.get_end_iter(),True)
        for method in ('watch_file','file_changed','check_external','reload_external','compare_external','keep_external','loaded'):
            setattr(surface,method,getattr(EditorPane,method).__get__(surface))
        surface.watch_file();self.addCleanup(self.finish)

    def finish(self):
        self.editor._shutting_down=True
        self.watch.close();self.sources.close();self.work.stop();self.work.join();self.watch.join()

    def external_write(self, content):
        self.path.write_text(content)
        wait_for(lambda:self.editor.external.snapshot is not None)

    def test_clean_file_not_automatically_reloaded_and_compare_then_reload(self):
        self.external_write('external\n')
        self.assertEqual(self.editor.text(),'original\n')
        self.assertIn('File changed externally',self.editor.status.text)
        self.editor.compare_external()
        self.assertEqual(self.views[0][0],'local')
        self.assertIn('-external',self.views[0][2]);self.assertIn('+original',self.views[0][2])
        self.editor.reload_external()
        self.assertEqual(self.editor.text(),'external\n')
        self.assertFalse(self.editor.dirty)
        self.assertIsNone(self.editor.external.snapshot)

    def test_dirty_conflict_keeps_both_and_reload_requires_confirmation(self):
        self.editor.buffer.set_text('unsaved\n')
        self.external_write('external\n')
        self.assertEqual(self.editor.text(),'unsaved\n')
        self.assertEqual(self.editor.external.snapshot.content,'external\n')
        self.assertIn('unsaved',self.editor.status.text)
        self.editor.reload_external();self.assertEqual(self.editor.text(),'unsaved\n')
        self.confirmations.pop()();self.assertEqual(self.editor.text(),'external\n')

    def test_keep_my_version_is_explicit_and_save_still_checks_revision(self):
        self.editor.buffer.set_text('unsaved\n')
        self.external_write('external\n')
        original=self.editor.revision
        self.editor.keep_external();self.assertEqual(self.editor.revision,original)
        self.confirmations.pop()()
        self.assertEqual(self.editor.revision,self.editor.external.snapshot.revision)
        self.assertEqual(self.editor.text(),'unsaved\n')
        self.path.write_text('new external update\n')
        from naui.backends import ConflictError
        with self.assertRaises(ConflictError):self.backend.write(str(self.path),self.editor.text(),self.editor.revision)
        self.assertEqual(self.path.read_text(),'new external update\n')

    def test_external_rename_keeps_device_buffer_and_updates_lsp_identity(self):
        self.editor.buffer.set_text('unsaved\n')
        target=self.root/'renamed.txt';self.path.rename(target)
        wait_for(lambda:self.editor.path==str(target))
        self.assertEqual(self.editor.device_id,'local')
        self.assertIs(self.editor.backend,self.backend)
        self.assertEqual(self.editor.text(),'unsaved\n')
        self.assertEqual(self.moved,[(str(self.path),str(target))])

    def test_deleted_file_keeps_buffer_and_disables_keep_overwrite(self):
        self.editor.buffer.set_text('unsaved\n');self.path.unlink()
        wait_for(lambda:self.editor.external.deleted)
        self.assertEqual(self.editor.text(),'unsaved\n')
        self.assertFalse(self.editor.keep_button.sensitive)
        self.editor.reload_external();self.assertEqual(self.editor.text(),'unsaved\n')


class WatchPermissionTests(unittest.TestCase):
    def test_remote_watch_command_and_terminal_operations_require_separate_grants(self):
        from naui.plugin_api import PluginAPI
        workspace=Workspace('/tmp')
        device=workspace.add_ssh('server')
        host=SimpleNamespace(_shutting_down=False,workspace=workspace)
        api=PluginAPI(host,'test',{'filesystem_read','filesystem_watch'})
        with self.assertRaises(PermissionError):api.files.watch(device_id=device.id,path='/project',callback=lambda event:None)
        with self.assertRaises(PermissionError):api.commands.execute(device_id='local',cwd='/tmp',command=['git','status'],callback=lambda *_:None)
        with self.assertRaises(PermissionError):api.terminals.run(device_id='local',cwd='/tmp',command=['python3','test.py'])

    def test_plugin_cannot_interrupt_another_plugins_run(self):
        from naui.plugin_api import PluginAPI
        host=SimpleNamespace(_shutting_down=False)
        api=PluginAPI(host,'test',{'terminal_execution'})
        with self.assertRaises(PermissionError):api.terminals.interrupt('another-plugins-id')


class ReactiveGitTests(unittest.TestCase):
    def setUp(self):
        from naui.plugin_manager import PluginManager, PluginSpec
        from naui.plugins.base_git import start
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=self.root/'repo';self.repo.mkdir()
        local_command([*GIT,'init','-q','-b','main'],str(self.repo))
        local_command([*GIT,'config','user.name','Naui Test'],str(self.repo))
        local_command([*GIT,'config','user.email','naui-test@example.invalid'],str(self.repo))
        self.file=self.repo/'file.py';self.file.write_text('original=1\n')
        local_command([*GIT,'add','--','file.py'],str(self.repo));local_command([*GIT,'commit','-qm','Initial'],str(self.repo))
        self.workspace=Workspace(str(self.repo));self.sources=Sources();self.work=FileWork(self.sources)
        self.watch=FileWatchService(self.sources,self.workspace.devices.__getitem__)
        self.command_service=CommandService();self.decorations,self.sections,self.views,self.badges=[],[],[],[]
        def sidebar(*args):
            self.sections.append(args)
            self.host.sidebar_sections[args[:2]] = None
        self.host=SimpleNamespace(_shutting_down=False,workspace=self.workspace,sources=self.sources,work=self.work.submit,
            file_watch=self.watch,command_service=self.command_service,set_status=lambda message:None,
            plugin_sidebar=sidebar, sidebar_sections={}, plugin_sidebar_badge=lambda *args:self.badges.append(args),
            open_text_view=lambda *args:self.views.append(args),
            open_diff_view=lambda *args, **kwargs:self.views.append(args))
        self.host.workspace_sidebar=SimpleNamespace(explorers={'local':SimpleNamespace(set_git_status=lambda *args:self.decorations.append(args))},
            active_explorer=SimpleNamespace(device=self.workspace.devices['local'],selected_path=lambda:str(self.repo)))
        self.host.plugins=self.manager=PluginManager(self.host)
        permissions={'commands','filesystem_read','filesystem_watch','remote_access','command_execution','editor_navigation','views'}
        self.api=self.manager.activate(PluginSpec('base-git','Git',frozenset(permissions),True,True),start,permissions)
        self.plugin=self.manager.plugins['base-git'][1]
        self.manager.emit('workspace')
        wait_for(lambda:bool(self.plugin.repositories) and next(iter(self.plugin.repositories.values())).status is not None)
        self.addCleanup(self.finish)

    def finish(self):
        self.host._shutting_down=True;self.manager.close();self.watch.close();self.command_service.close()
        self.work.stop();self.watch.join();self.work.join();self.sources.close()

    def test_file_event_reactively_refreshes_git_decorations_and_diff_on_same_device(self):
        self.file.write_text('modified=2\n')
        wait_for(lambda:self.decorations[-1][2].get(str(self.file))=='M')
        repo=self.plugin.repositories[('local',str(self.repo))]
        change=next(item for item in repo.status.changes if item.path=='file.py')
        self.plugin.diff(repo,change)
        wait_for(lambda:bool(self.views))
        self.assertEqual(self.views[0][0],'local')
        self.assertEqual(self.badges[-1], ('base-git', 'status', 1))
        self.assertIn('+modified=2',self.views[0][2])
        rows=self.sections[-1][-1]
        children=next(row for row in rows if row['text']=='CHANGES')['children']
        group=next(row for row in children if row['text'].startswith('Changes'))
        self.assertEqual(group['children'][0]['text'],'file.py')
        self.assertEqual(group['children'][0]['badge'],'M')
        self.assertIn('Discard Changes…',[label for label, cb in group['children'][0]['menu']])

    def test_expanded_directory_events_share_service_and_refresh_git(self):
        folder=self.repo/'src';folder.mkdir();file=folder/'code.py';file.write_text('original=1\n')
        local_command([*GIT,'add','--','src/code.py'],str(self.repo));local_command([*GIT,'commit','-qm','Add nested fixture'],str(self.repo))
        handle=self.watch.watch(FileLocation('local',str(folder)),lambda event:None)
        file.write_text('modified=2\n')
        wait_for(lambda:self.decorations[-1][2].get(str(file))=='M')
        handle.close()

    def test_removing_last_repository_root_releases_watchers_but_retains_cached_status(self):
        repo=self.plugin.repositories[('local',str(self.repo))]
        self.assertTrue(repo.watches)
        device=self.workspace.devices['local'];device.roots.clear();device.observe(str(self.root))
        self.manager.emit('workspace')
        self.assertFalse(repo.active);self.assertFalse(repo.watches)
        wait_for(lambda:not self.work._futures and not self.work._callbacks)
        self.assertFalse(repo.watches)
        self.assertIsNotNone(repo.status)

    def test_repository_detection_cached_instead_of_repeated_for_every_item(self):
        repo=self.plugin.repositories[('local',str(self.repo))]
        known=dict(self.plugin.detected)
        for _ in range(30):self.manager.emit('workspace')
        self.assertEqual(self.plugin.detected,known)
        self.assertIs(self.plugin.repositories[('local',str(self.repo))],repo)

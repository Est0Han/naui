"""Development workflow boundaries on disposable files and repositories."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import gi
for name, version in [('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5')]: gi.require_version(name,version)
from naui.commands import CommandService, local_command
from naui.backends import LocalFileBackend, RemoteFileBackend
from naui.development import Development, atomic_json
from naui.git_hunks import select_hunk
from naui.replace_worker import replace
from naui.session_restore import SessionRestore
from naui.plugins.base_filepath import path_links
from test_workspace import RootedTransport

class DevelopmentTests(unittest.TestCase):
    def test_atomic_drafts_are_private_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'session.json'
            data=dict(version=1,layout='horizontal',files=[dict(device='local',path='/tmp/a',revision='missing',offset=2,group=1,dirty=True,pinned=True,text='draft')])
            SessionRestore.validate(data); atomic_json(path,data)
            self.assertEqual(json.loads(path.read_text()),data)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            data['files'][0]['text']=None
            with self.assertRaises(ValueError): SessionRestore.validate(data)
    def test_task_argv_remains_literal(self):
        task=dict(name='Test',device='local',cwd='/tmp/project with spaces',argv=['printf','%s','$(touch unwanted)'])
        Development.validate_task(task)
        with self.assertRaises(ValueError): Development.validate_task(dict(task,cwd='relative'))
    def test_replace_cases_groups_lines_and_literal_dollars(self):
        self.assertEqual(replace('cat CAT scatter\n','cat','$1',word=True),('$1 $1 scatter\n',2))
        self.assertEqual(replace('x=12\nx=34\n',r'x=(\d+)',r'number=\1',regex=True),('number=12\nnumber=34\n',2))
        self.assertEqual(replace('a\r\na\n','^a','b',regex=True),('b\r\nb\n',2))
    def test_compiler_and_traceback_links_keep_device_and_position(self):
        for message,line,column in [('src/Foo.cs(12,3): error',11,2),('src/foo.py:9:2: failure',8,1),('File "src/foo.py", line 8',7,0)]:
            links=path_links(message,'/project','remote',remote=True)
            self.assertEqual(len(links),1,message)
            self.assertEqual((links[0].location.device_id,links[0].location.line,links[0].location.character),('remote',line,column))
    def test_git_stages_one_block_and_unstages_without_touching_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def git(*args, **kwargs): return local_command(['git',*args],directory,**kwargs)
            git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
            path=root/'file.txt';original=''.join(f'line {i}\n' for i in range(40));path.write_text(original)
            git('add','.');git('commit','-qm','initial')
            changed=original.replace('line 2\n','first change\n').replace('line 35\n','second change\n');path.write_text(changed)
            patch=git('diff','--no-ext-diff').decode()
            block=select_hunk(patch,0).encode()
            git('apply','--cached','-',input_data=block)
            staged=git('diff','--cached').decode()
            self.assertIn('first change',staged);self.assertNotIn('second change',staged)
            git('apply','--cached','--reverse','-',input_data=block)
            self.assertEqual(git('diff','--cached'),b'');self.assertEqual(path.read_text(),changed)
            with self.assertRaises(ValueError): select_hunk(patch,-1)
    def test_command_input_uses_remote_transport_and_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=RemoteFileBackend(RootedTransport('test',directory))
            try:
                service=CommandService()
                data=b'hello\n'*20000
                self.assertEqual(service.execute(backend,['cat'],'/',input_data=data),data)
                with self.assertRaises(ValueError): service.execute(backend,['cat'],'/',input_data=b'x'*(8*1024*1024+1))
            finally: backend.close()

    def test_language_server_requests_use_protocol_specific_parameters(self):
        from naui.plugins.base_lsp.workspace import LspSession
        from unittest.mock import Mock
        import threading
        session=LspSession.__new__(LspSession)
        session._operations=threading.RLock()
        session._sync=Mock(return_value='file:///project/a.py')
        session.client=SimpleNamespace(capabilities={'completionProvider':{},'documentFormattingProvider':True,'codeActionProvider':{}},request=Mock(return_value=[]))
        session.query('formatting','/project/a.py','x=1',1,{'line':0,'character':0},options={'tabSize':4,'insertSpaces':True})
        method,parameters=session.client.request.call_args.args
        self.assertEqual(method,'textDocument/formatting');self.assertNotIn('position',parameters)
        session.query('completion','/project/a.py','x=1',1,{'line':0,'character':1})
        self.assertEqual(session.client.request.call_args.args[1]['position']['character'],1)

    def test_recovery_preserves_draft_after_device_removal(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            editor=Mock(loading=False,revision='original',dirty=True,device_id='removed-device',path='/project/file.py')
            editor.text.return_value='unsaved remote draft'
            editor.buffer.get_iter_at_mark.return_value.get_offset.return_value=3
            group=object()
            tabs=SimpleNamespace(pages=[editor],groups=[group],owner=lambda _:group,pinned=set(),paned=None)
            window=SimpleNamespace(workspace_store=SimpleNamespace(path=Path(directory)/'workspace.json'),
                workspace=SimpleNamespace(devices={}),editors=[editor],notebook=tabs,current_editor=lambda:editor)
            recovery=SessionRestore(window);recovery.save()
            self.assertEqual(json.loads(recovery.path.read_text())['files'][0]['text'],'unsaved remote draft')

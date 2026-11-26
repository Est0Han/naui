"""Search acceptance with disposable workspaces and local simulated SSH channels."""
import base64
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from naui.backends import LocalFileBackend, RemoteFileBackend
from naui.search_model import (DEFAULT_EXCLUDES, SearchQuery, SearchResult, SearchRoot,
    allowed, file_score, glob_matches, parse_rg, patterns, preview, rg_arguments)
from naui.search_service import (FilePathCache, LocalStream, SearchBackend, SearchCancelled,
    SearchSession, WorkspaceSearchService)
from naui.ssh import Connection


def collect(service, query, timeout=12):
    session = service.start(query); values = []; deadline = time.monotonic() + timeout
    while session.state in {'pending','running'} or not session.events.empty():
        values.extend(session.drain())
        if time.monotonic() > deadline:
            service.cancel(); raise AssertionError('Search did not finish: ' + session.state)
        time.sleep(.005)
    return session, [value for kind,value in values if kind == 'result'], [value for kind,value in values if kind == 'warning']


class ModelTests(unittest.TestCase):
    def test_queries_and_invalid_regex(self):
        self.assertEqual(SearchQuery('text').max_file_size,10*1024*1024)
        for text in ('','\0','x'*4097):
            with self.assertRaises(ValueError): SearchQuery(text)
        with self.assertRaisesRegex(ValueError,'Invalid regex'): SearchQuery('[',regex=True)
        with self.assertRaises(ValueError): SearchQuery('x',max_results=10001)
        with self.assertRaises(ValueError): SearchRoot('local','relative',object())
        with self.assertRaises(ValueError): patterns('!foo')
        self.assertEqual(patterns(' *.py, *.toml '),('*.py','*.toml'))

    def test_globs_and_pruning(self):
        for path,glob in [('main.py','*.py'),('src/main.py','*.py'),('src/main.py','src/**'),('src/a/b.py','src/**'),('build/','**/build/**'),('a/build/','**/build/**')]:
            self.assertTrue(glob_matches(path,glob),(path,glob))
        self.assertFalse(glob_matches('src/a/b.py','src/*'))
        self.assertFalse(glob_matches('src/a/b.py','src/a?b.py'))
        self.assertTrue(glob_matches('src/a/b.py','src/[a-z]/b.?y'))
        self.assertFalse(glob_matches('another.py','src/**'))
        query=SearchQuery('x',include_patterns=('*.py',))
        self.assertTrue(allowed('src',query,True))
        self.assertFalse(allowed('build',query,True))
        self.assertFalse(allowed('.hidden.py',query))
        self.assertFalse(allowed('main.txt',query))
        self.assertTrue(allowed('main.py',query))
        self.assertTrue(allowed('.hidden.py',SearchQuery('x',include_hidden=True)))

    def test_fuzzy_ranking_paths_and_unicode(self):
        self.assertGreater(file_score('device.py','device.py'),file_score('dev','device-long.py'))
        self.assertGreater(file_score('dev','device.py'),file_score('dev','test_device.py'))
        self.assertGreater(file_score('tdev','tests/test_device.py'),file_score('tdev','test/deep/environment/value.txt'))
        self.assertIsNotNone(file_score('ü','src/über.py'))
        self.assertIsNotNone(file_score('src/auth','src/authentication.py'))
        self.assertIsNone(file_score('zzzzz','src/device.py'))
        self.assertIsNone(file_score('DEV','src/device.py',case_sensitive=True))

    def test_rg_json_unicode_byte_offsets_and_base64_path(self):
        root=SearchRoot('remote-a','/opt/example',object(),True)
        value={'type':'match','data':{'path':{'bytes':base64.b64encode('src/über.py'.encode()).decode()},'lines':{'text':'😀 café connect\n'},'line_number':42,
            'submatches':[{'start':len('😀 café '.encode()),'end':len('😀 café connect'.encode())}]}}
        result=parse_rg(json.dumps(value),root)[0]
        self.assertEqual((result.device_id,result.logical_path,result.line,result.column,result.match_end),('remote-a','/opt/example/src/über.py',42,7,14))
        self.assertEqual(parse_rg('{"type":"summary"}',root),[])
        value['data']['path']={'text':'../escape.py'}
        with self.assertRaises(ValueError): parse_rg(json.dumps(value),root)
        self.assertLessEqual(len(preview('a'*200000+'match',200000,200005)),242)

    def test_safe_rg_arguments(self):
        query=SearchQuery("'; $(touch nope); #",include_patterns=('*.py','src/**'),exclude_patterns=('build/**',),case_sensitive=True)
        argv=rg_arguments(query)
        self.assertEqual(argv[-3:],('--',query.text,'.'))
        self.assertIn('--fixed-strings',argv)
        self.assertNotIn('--ignore-case',argv)
        self.assertIn('!build/**',argv)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='naui-search-test-');self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.backend=LocalFileBackend();self.root=SearchRoot('local',str(self.path),self.backend)
        self.service=WorkspaceSearchService();self.addCleanup(self.service.join);self.addCleanup(self.service.close)
        (self.path/'src').mkdir();(self.path/'tests').mkdir();(self.path/'empty-device').mkdir()
        (self.path/'src/device.py').write_text('connect connect\nCONNECT connected\ncafé 😀 connect\n',encoding='utf-8')
        (self.path/'tests/test_device.py').write_text('connect\n',encoding='utf-8')
        (self.path/'tests/über.py').write_text('connect\n',encoding='utf-8')
        (self.path/'binary.bin').write_bytes(b'connect\0binary')
        (self.path/'invalid.txt').write_bytes(b'\xff connect')
        (self.path/'large.txt').write_text('connect '*100)
        (self.path/'.hidden.py').write_text('connect\n')
        (self.path/'build').mkdir();(self.path/'build/large.py').write_text('connect\n')
        (self.path/'ignored.txt').write_text('connect\n')
        (self.path/'.gitignore').write_text('ignored.txt\nignored-dir/\n')
        (self.path/'ignored-dir').mkdir();(self.path/'ignored-dir/test.py').write_text('connect\n')
        subprocess.run(['git','init','-q',str(self.path)],check=True)

    def query(self,text='connect',**kwargs): return SearchQuery(text,roots=(self.root,),**kwargs)

    def content(self,query,fallback=False):
        with patch('naui.search_service.shutil.which',return_value=None) if fallback else patch.dict(os.environ,{}):
            return collect(self.service,query)

    def assert_content(self,fallback):
        session,results,warnings=self.content(self.query(max_file_size=100),fallback)
        self.assertEqual(session.state,'completed')
        self.assertEqual(len(results),7)
        self.assertEqual({r.relative_path for r in results},{'src/device.py','tests/test_device.py','tests/über.py'})
        self.assertEqual(len([r for r in results if r.line==1 and r.relative_path=='src/device.py']),2)
        self.assertTrue(all(len(r.preview)<=242 for r in results))
        self.assertEqual(len(warnings),1 if fallback else 0)

    @unittest.skipUnless(shutil.which('rg'),'ripgrep unavailable')
    def test_ripgrep_content_binary_large_hidden_ignored(self): self.assert_content(False)

    def test_compatibility_content_binary_large_hidden_ignored(self): self.assert_content(True)

    def test_content_options_and_regex_fallback(self):
        _,results,_=self.content(self.query(case_sensitive=True,whole_word=True,max_file_size=100),True)
        self.assertEqual(len(results),5)
        _,results,_=self.content(self.query(r'con(?:nect|nected)',regex=True,max_file_size=100),True)
        self.assertEqual(len(results),7)
        self.assertTrue(all(r.device_id=='local' for r in results))

    def test_include_exclude_filters_consistent(self):
        for fallback in (True,False) if shutil.which('rg') else (True,):
            self.service.capabilities.clear()
            _,results,_=self.content(self.query(include_patterns=('src/**',),exclude_patterns=DEFAULT_EXCLUDES+('*.txt',)),fallback)
            self.assertEqual({r.relative_path for r in results},{'src/device.py'})

    def test_hidden_ignored_override_and_explicit_excludes(self):
        for fallback in (True,False) if shutil.which('rg') else (True,):
            self.service.capabilities.clear()
            _,results,_=self.content(self.query(include_hidden=True,include_ignored=True,max_file_size=100),fallback)
            paths={r.relative_path for r in results}
            self.assertTrue({'.hidden.py','ignored.txt','ignored-dir/test.py'}.issubset(paths))
            self.assertNotIn('build/large.py',paths)

    def test_tracked_ignored_file_matches_ripgrep_ignore_semantics(self):
        subprocess.run(['git','-C',str(self.path),'add','-f','ignored.txt'],check=True)
        for fallback in (True,False) if shutil.which('rg') else (True,):
            self.service.capabilities.clear()
            _,results,_=self.content(self.query(max_file_size=100),fallback)
            self.assertNotIn('ignored.txt',{result.relative_path for result in results})

    def test_files_fuzzy_empty_folders_duplicate_names_unicode(self):
        (self.path/'tests/device.py').write_text('')
        _,results,_=collect(self.service,self.query('dev',mode='files'))
        self.assertTrue({'src/device.py','tests/device.py','tests/test_device.py','empty-device'}.issubset({r.relative_path for r in results}))
        self.assertTrue(next(r for r in results if r.relative_path=='empty-device').directory)
        _,results,_=collect(self.service,self.query('tdev',mode='files'))
        self.assertIn('tests/test_device.py',{r.relative_path for r in results})
        _,results,_=collect(self.service,self.query('ü',mode='files'))
        self.assertEqual([r.relative_path for r in results],['tests/über.py'])

    def test_file_cache_expiry_invalidation_and_bound(self):
        collect(self.service,self.query('dev',mode='files'))
        with patch.object(self.backend,'iter_directory',side_effect=AssertionError('rescan')):
            _,results,_=collect(self.service,self.query('device',mode='files'))
            self.assertTrue(results)
        (self.path/'new-device.py').write_text('')
        self.service.file_cache.invalidate()
        _,results,_=collect(self.service,self.query('device',mode='files'))
        self.assertIn('new-device.py',{r.relative_path for r in results})
        cache=FilePathCache();cache.put(('key',),list(range(20001)),0);self.assertFalse(cache.values)
        cache.put(('key',),[1],0)
        with patch('naui.search_service.time.monotonic',return_value=time.monotonic()+6): self.assertIsNone(cache.get(('key',)))

    def test_no_symlink_escape_or_cycles(self):
        (self.path/'src/cycle').symlink_to(self.path,target_is_directory=True)
        (self.path/'alias.py').symlink_to(self.path/'src/device.py')
        for fallback in (True,False) if shutil.which('rg') else (True,):
            self.service.capabilities.clear()
            _,results,_=self.content(self.query(max_file_size=100),fallback)
            self.assertFalse(any('alias' in r.relative_path or 'cycle' in r.relative_path for r in results))

    def test_result_and_metadata_limits(self):
        session,results,_=collect(self.service,self.query(max_results=2))
        self.assertEqual(len(results),2);self.assertTrue(session.limited)
        session,results,_=collect(self.service,self.query('dev',mode='files',max_entries=2))
        self.assertTrue(session.limited);self.assertLessEqual(session.entries,3)

    def test_literal_shell_metacharacters_are_data(self):
        query="'; $(touch injected); #"
        (self.path/'quoted.py').write_text(query)
        _,results,_=collect(self.service,self.query(query))
        self.assertEqual(len(results),1);self.assertFalse((self.path/'injected').exists())

    def test_multiple_device_roots_keep_identity(self):
        roots=tuple(SearchRoot(device,str(self.path),self.backend) for device in ('local','remote-a','remote-b'))
        _,results,_=collect(self.service,SearchQuery('device',mode='files',roots=roots))
        matches=[r for r in results if r.relative_path=='src/device.py']
        self.assertEqual({r.device_id for r in matches},{'local','remote-a','remote-b'})
        self.assertEqual(len({r.identity for r in matches}),3)

    def test_permission_error_does_not_abort_other_roots(self):
        missing=SearchRoot('other',str(self.path/'missing'),self.backend)
        _,results,warnings=collect(self.service,SearchQuery('connect',roots=(missing,self.root),max_file_size=100))
        self.assertTrue(results);self.assertTrue(warnings)

    def test_git_repository_exclude_and_global_ignore(self):
        (self.path/'repo-excluded.py').write_text('connect')
        (self.path/'globally-excluded.py').write_text('connect')
        (self.path/'.git/info/exclude').write_text('repo-excluded.py\n')
        global_ignore=self.path/'global-ignore';global_ignore.write_text('globally-excluded.py\n')
        # Both Git and ripgrep read ~/.gitconfig; ripgrep does not implement
        # Git's GIT_CONFIG_GLOBAL environment override.
        global_config=self.path/'.gitconfig'
        subprocess.run(['git','config','--file',str(global_config),'core.excludesFile',str(global_ignore)],check=True)
        with patch.dict(os.environ,HOME=str(self.path),GIT_CONFIG_GLOBAL=str(global_config)):
            for fallback in (True,False) if shutil.which('rg') else (True,):
                self.service.capabilities.clear()
                _,results,_=self.content(self.query(max_file_size=100),fallback)
                paths={result.relative_path for result in results}
                self.assertNotIn('repo-excluded.py',paths);self.assertNotIn('globally-excluded.py',paths)

    def test_huge_directory_pruned_without_reading_contents(self):
        with patch.object(self.backend,'transfer_reader',side_effect=AssertionError('Content read during file search')):
            _,results,_=collect(self.service,self.query('device',mode='files'))
            self.assertTrue(results)
        class HugeBackend:
            def __init__(self): self.enumerated=0
            def iter_directory(self,path,control):
                from naui.filesystem import FileEntry
                for index in range(1000000):
                    self.enumerated+=1
                    yield FileEntry(str(index),'/project/'+str(index),'f',0,0,0,'','revision')
        huge=HugeBackend();root=SearchRoot('local','/project',huge)
        session,_,_=collect(self.service,SearchQuery('never-matches',mode='files',include_ignored=True,max_entries=50,roots=(root,)))
        self.assertTrue(session.limited);self.assertEqual(huge.enumerated,51)


class RemoteTests(WorkspaceTests):
    """OpenSSH is mocked as a local Bash transport; real stream/EOF/parser used."""
    def setUp(self):
        super().setUp()
        self.remote=RemoteFileBackend(Connection('example.invalid'))
        self.addCleanup(self.remote.close)
        self.addCleanup(self.service.close)
        self.root=SearchRoot('remote-a',str(self.path),self.remote,True,'Example Device')
        self.connection_patch=patch.object(Connection,'command',lambda connection,script,**kwargs:['bash','-c',script])
        self.connection_patch.start();self.addCleanup(self.connection_patch.stop)

    @unittest.skipUnless(shutil.which('rg'),'ripgrep unavailable')
    def test_remote_rg_structured_paths_and_capability_cache(self):
        with patch.object(SearchBackend,'has_rg',autospec=True,return_value=True) as detect:
            _,results,_=collect(self.service,self.query(max_file_size=100))
            _,again,_=collect(self.service,self.query('CONNECT',case_sensitive=True,max_file_size=100))
            self.assertEqual(detect.call_count,1)
        self.assertEqual({r.device_id for r in results},{'remote-a'})
        self.assertTrue(again);self.assertFalse(self.remote._processes)

    def test_remote_no_rg_uses_existing_bounded_backend(self):
        with patch.object(SearchBackend,'has_rg',return_value=False):
            _,results,warnings=collect(self.service,self.query(max_file_size=100))
        self.assertEqual(len(results),7)
        self.assertTrue(any('compatibility' in warning for warning in warnings))
        self.assertFalse(self.remote._processes)

    def test_safe_remote_root_query_and_filters(self):
        special=self.path / "root '; $shell with spaces";special.mkdir()
        (special/'special file.py').write_text("'; $(touch injected); #")
        root=SearchRoot('remote-a',str(special),self.remote,True)
        _,results,_=collect(self.service,SearchQuery("'; $(touch injected); #",include_patterns=('*.py',),roots=(root,)))
        self.assertEqual(len(results),1);self.assertEqual(results[0].logical_path,str(special/'special file.py'))
        self.assertFalse((special/'injected').exists());self.assertFalse(self.remote._processes)

    def test_owned_remote_cancel_does_not_kill_unrelated_process(self):
        session=SearchSession(1,self.query())
        backend=SearchBackend(self.root)
        unrelated=subprocess.Popen(['sleep','30']);self.addCleanup(lambda:unrelated.wait())
        self.addCleanup(unrelated.terminate)
        with backend.stream(('sh','-c','printf ready; sleep 30'),session) as stream:
            self.assertEqual(stream.read(5),b'ready')
            session.cancel();session.cancel()
            with self.assertRaises(SearchCancelled): stream.read(5)
        self.assertIsNone(unrelated.poll());self.assertFalse(self.remote._processes)

    def test_remote_disconnect_mid_search(self):
        self.remote.connected=False
        _,results,warnings=collect(self.service,self.query())
        self.assertFalse(results);self.assertTrue(any('disconnected' in warning.lower() for warning in warnings))
        self.assertFalse(self.remote._processes)

    def test_simultaneous_local_and_two_remote_devices(self):
        second=RemoteFileBackend(Connection('another-example.invalid'))
        try:
            roots=(SearchRoot('local',str(self.path),self.backend),self.root,
                   SearchRoot('remote-b',str(self.path),second,True))
            _,results,_=collect(self.service,SearchQuery('connect',max_file_size=100,roots=roots))
            self.assertEqual({result.device_id for result in results},{'local','remote-a','remote-b'})
            self.assertFalse(self.remote._processes);self.assertFalse(second._processes)
        finally:second.close()

    # Local-specific tests rely on self.backend; exercise them only in parent.
    test_file_cache_expiry_invalidation_and_bound = None
    test_multiple_device_roots_keep_identity = None
    test_compatibility_content_binary_large_hidden_ignored = None
    test_content_options_and_regex_fallback = None
    test_permission_error_does_not_abort_other_roots = None


class LifecycleTests(unittest.TestCase):
    def test_expired_session_finishes_failed_with_inline_warning(self):
        service=WorkspaceSearchService()
        try:
            session=SearchSession(1,SearchQuery('x'))
            session.deadline=time.monotonic()-1
            service._run(session)
            self.assertEqual(session.state,'failed')
            self.assertTrue(any('timed out' in warning for warning in session.warnings))
        finally:service.close();service.join()

    def test_rapid_queries_cancel_and_no_stale_callbacks(self):
        class SlowBackend:
            def __init__(self,root): self.root=root
            def files(self,session):
                for index in range(100):
                    session.check();time.sleep(.002)
                    session.result(SearchResult('local','/project','/project/'+session.query.text))
        service=WorkspaceSearchService(SlowBackend)
        root=SearchRoot('local','/project',object())
        sessions=[]
        try:
            for index in range(60):
                sessions.append(service.start(SearchQuery(str(index),mode='files',roots=(root,))))
                time.sleep(.001)
            latest=sessions[-1];results=[];deadline=time.monotonic()+4
            while latest.state in {'pending','running'} or not latest.events.empty():
                results.extend(value for kind,value in latest.drain() if kind=='result')
                self.assertLess(time.monotonic(),deadline);time.sleep(.01)
            self.assertTrue(results);self.assertTrue(all(result.logical_path=='/project/59' for result in results))
            self.assertTrue(all(session.state=='cancelled' for session in sessions[:-1]))
            latest.close();latest.close()
        finally: service.close();service.close();service.join()
        with self.assertRaises(RuntimeError):service.start(SearchQuery('x'))

    def test_full_result_queue_can_cancel(self):
        session=SearchSession(1,SearchQuery('x'))
        for _ in range(512):session.events.put(('result',None))
        finished=threading.Event()
        def worker():
            try:session.emit('result',None)
            except SearchCancelled:finished.set()
        thread=threading.Thread(target=worker);thread.start();session.cancel()
        thread.join(1);self.assertTrue(finished.is_set());self.assertFalse(thread.is_alive())

    def test_regex_worker_cancellable_in_catastrophic_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);(path/'input.txt').write_text('a'*100000+'!')
            service=WorkspaceSearchService();root=SearchRoot('local',directory,LocalFileBackend())
            try:
                with patch.object(SearchBackend,'has_rg',return_value=False):
                    session=service.start(SearchQuery('(a+)+$',regex=True,roots=(root,)))
                    time.sleep(.2);session.cancel();session.cancel();service.close()
                    session.future.result(timeout=3)
                    self.assertEqual(session.state,'cancelled')
            finally:service.close();service.join()


if __name__ == '__main__': unittest.main()

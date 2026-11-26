import sys, tempfile, time, unittest
from naui.process_protocol import Client

class TransportTests(unittest.TestCase):
    def test_real_stdio_roundtrip_and_process_cleanup(self):
        program = '''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' in m: print(json.dumps({'id':m['id'],'result':{'ok':True}}),flush=True)
'''
        with tempfile.TemporaryDirectory() as cwd:
            client=Client(cwd,command=[sys.executable,'-u','-c',program]); results=[]
            try:
                client.request('initialize',{},lambda r,e:results.append((r,e)))
                deadline=time.monotonic()+3
                while not results and time.monotonic()<deadline:
                    client.drain(lambda _:None);time.sleep(.01)
                self.assertEqual(results,[({'ok':True},None)])
            finally: client.close()
            deadline=time.monotonic()+3
            while client.process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
            self.assertIsNotNone(client.process.poll())
            self.assertFalse(client.pending)



from types import SimpleNamespace
from unittest.mock import Mock, patch
from naui.plugin_api import ProcessAPI

class ProcessAPITests(unittest.TestCase):
    def test_permission_precedes_process_start(self):
        denied=Mock(side_effect=PermissionError('missing command permission'))
        api=ProcessAPI(SimpleNamespace(require=denied))
        with patch('naui.process_protocol.Client') as client:
            with self.assertRaises(PermissionError): api.json_lines(cwd='/tmp',command=['ignored'])
            client.assert_not_called()

    def test_explicit_command_and_owner_cleanup(self):
        with tempfile.TemporaryDirectory() as cwd:
            api=ProcessAPI(SimpleNamespace(require=Mock()))
            with patch('naui.process_protocol.Client') as client:
                with self.assertRaises(ValueError): api.json_lines(cwd=cwd,command=[])
                client.assert_not_called()
                handle=api.json_lines(cwd=cwd,command=['example','--stdio'])
                client.assert_called_once_with(cwd,command=['example','--stdio'])
                api.close();handle.close.assert_called_once()
                self.assertFalse(api.clients)

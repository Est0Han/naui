"""Local CLI -> SSH helper contracts without a model or real SSH server."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from naui.ssh_workspace import prepare_workspace, remote_command
from naui.ssh import Connection

class AgentSSHTests(unittest.TestCase):
    def test_quote_remote_arguments_and_keep_transport(self):
        command = remote_command(Connection('user@host', ('-p','2222','-J','jump')), "/project's folder", ['printf', '%s', '$(touch /tmp/never); x'])
        self.assertIn('BatchMode=yes', command)
        self.assertIn('2222', command)
        self.assertIn('jump', command)
        self.assertEqual(command[-2], 'user@host')
        self.assertIn("'$(touch /tmp/never); x'", command[-1])
        with self.assertRaises(ValueError): remote_command(Connection('host'), 'relative', ['pwd'])

    def test_reconnect_reuses_saved_workspace_and_refreshes_socket(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_CACHE_HOME':directory}):
            device=NS(id='ssh-project',filesystem_backend=NS(connection=Connection('user@host', ('-S','/tmp/old-socket'))))
            saved=prepare_workspace('test-plugin',device,'/project')
            device.filesystem_backend.connection=Connection('user@host',('-S','/tmp/new-socket'))
            other=prepare_workspace('test-plugin',device,'/project')
            self.assertNotEqual(other['cwd'], saved['cwd'])
            resumed=prepare_workspace('test-plugin',device,'/project',existing=saved['cwd'])
            self.assertEqual(resumed['cwd'],saved['cwd'])
            self.assertEqual(resumed['command'],saved['command'])
            data=json.loads((Path(saved['cwd'])/'connection.json').read_text())
            self.assertEqual(data['options'],['-S','/tmp/new-socket'])
            with self.assertRaises(ValueError): prepare_workspace('other-plugin',device,'/project',existing=saved['cwd'])
            with self.assertRaises(ValueError): prepare_workspace('test-plugin',device,'/another-project',existing=saved['cwd'])
            device.filesystem_backend.connection=Connection('other@host')
            with self.assertRaises(ValueError): prepare_workspace('test-plugin',device,'/project',existing=saved['cwd'])

    def test_helper_reads_and_edits_remote_fixture_through_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root/'remote project'; remote.mkdir()
            (remote/'file.txt').write_text('before')
            binaries = root/'bin'; binaries.mkdir()
            fake = binaries/'ssh'
            # Stand in for the transport only; run the actual quoted remote script.
            fake.write_text('#!/usr/bin/python3\nimport os,sys,subprocess\n'
                'assert not any(k.startswith(("NAUI_", "__NAUI_")) for k in os.environ)\n'
                'assert "BatchMode=yes" in sys.argv\n'
                'raise SystemExit(subprocess.call(["/bin/sh", "-c", sys.argv[-1]]))\n')
            fake.chmod(0o700)
            device = NS(id='ssh-project', filesystem_backend=NS(connection=Connection('fixture')))
            with patch.dict(os.environ, {'XDG_CACHE_HOME': str(root/'cache')}):
                workspace = prepare_workspace('test-plugin', device, str(remote))
            env = dict(os.environ, PATH=str(binaries)+':'+os.environ['PATH'], NAUI_SECRET='must-not-travel')
            read = subprocess.run(workspace['command']+['cat','file.txt'], env=env, capture_output=True, text=True)
            self.assertEqual((read.returncode, read.stdout), (0, 'before'), read.stderr)
            edit = subprocess.run(workspace['command']+['sh','-c', 'printf after > file.txt'], env=env, capture_output=True)
            self.assertEqual(edit.returncode, 0, edit.stderr)
            self.assertEqual((remote/'file.txt').read_text(), 'after')
            self.assertFalse((Path(workspace['cwd'])/'file.txt').exists())
            config = Path(workspace['cwd'])/'connection.json'
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(config.read_text())['cwd'], str(remote))

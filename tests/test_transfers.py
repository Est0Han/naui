"""Real local I/O + real transient Bash scripts on isolated virtual SSH machines."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from naui.backends import LocalFileBackend, RemoteFileBackend
from naui.transfers import FileLocation, TransferManager, FINAL_STATES
from naui.transfer_io import CHUNK_SIZE
from naui.workspace import Workspace
from test_workspace import RootedTransport


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError('Timed out waiting for transfer')


class TransfersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='naui-transfer-test-')
        self.root = Path(self.temp.name)
        self.workspace = Workspace(str(self.root))
        self.backends = []
        self.manager = TransferManager(self.workspace.devices.__getitem__)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.manager.join)
        self.addCleanup(self.close)

    def close(self):
        self.manager.close()
        for backend in self.backends:
            backend.close()

    def remote(self, host):
        device = self.workspace.add_ssh(host)
        directory = self.root / host
        directory.mkdir()
        backend = RemoteFileBackend(RootedTransport(host, directory))
        device.filesystem_backend = backend
        device.state = 'connected'
        self.backends.append(backend)
        return device, directory

    def copy(self, source, destination, source_id='local', target_id='local'):
        return self.manager.copy(FileLocation(source_id, str(source)), FileLocation(target_id, str(destination)))

    def finished(self, job):
        result = wait_for(lambda: (s if (s := self.manager.status(job))['state'] in FINAL_STATES else None))
        return result

    def test_local_binary_permissions_and_unicode_shell_characters(self):
        source = self.root / "source õ ' $(touch BAD);\n.bin"
        target = self.root / 'destination'
        source.write_bytes(bytes(range(256)) * 9000)
        source.chmod(0o640)
        result = self.finished(self.copy(source, target))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertTrue(source.exists())
        self.assertEqual(result['bytes_transferred'], source.stat().st_size)
        self.assertFalse(list(self.root.glob('.naui-partial-*')))
        self.assertFalse((self.root / 'BAD').exists())

    def test_zero_byte_file_can_publish(self):
        source = self.root / 'empty'; source.touch()
        result = self.finished(self.copy(source, self.root / 'copied'))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual((self.root / 'copied').read_bytes(), b'')

    def test_folder_progressive_listing_and_symlink_skip(self):
        source = self.root / 'source'; (source / 'a' / 'b').mkdir(parents=True)
        (source / 'a' / 'b' / 'file').write_text('Unicode õ')
        (source / 'loop').symlink_to(source, target_is_directory=True)
        os.mkfifo(source / 'fifo')
        result = self.finished(self.copy(source, self.root / 'target'))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual((self.root / 'target/a/b/file').read_text(), 'Unicode õ')
        self.assertEqual(result['skipped'], 2)
        self.assertFalse((self.root / 'target/loop').exists())
        self.assertFalse((self.root / 'target/fifo').exists())

    def test_conflict_cancel_preserves_both_files(self):
        source = self.root / 'source'; source.write_text('new')
        target = self.root / 'target'; target.write_text('old')
        job = self.copy(source, target)
        conflict = wait_for(lambda: self.manager.conflicts())[0]
        self.assertEqual(conflict.choices, ('cancel', 'keep_both', 'replace'))
        self.assertEqual(target.read_text(), 'old')
        self.manager.resolve_conflict(job, 'cancel')
        self.assertEqual(self.finished(job)['state'], 'cancelled')
        self.assertEqual(source.read_text(), 'new')
        self.assertEqual(target.read_text(), 'old')

    def test_keep_both_backend_naming(self):
        source = self.root / 'source'; source.write_text('new')
        target = self.root / 'config.json'; target.write_text('old')
        (self.root / 'config (1).json').write_text('also old')
        job = self.copy(source, target)
        wait_for(lambda: self.manager.conflicts())
        self.manager.resolve_conflict(job, 'keep_both')
        self.assertEqual(self.finished(job)['state'], 'completed')
        self.assertEqual(target.read_text(), 'old')
        self.assertEqual((self.root / 'config (1).json').read_text(), 'also old')
        self.assertEqual((self.root / 'config (2).json').read_text(), 'new')

    def test_replace_is_explicit_and_preserves_destination_permissions(self):
        source = self.root / 'source'; source.write_text('new')
        target = self.root / 'target'; target.write_text('old'); target.chmod(0o640)
        job = self.copy(source, target)
        wait_for(lambda: self.manager.conflicts())
        self.manager.resolve_conflict(job, 'replace')
        self.assertEqual(self.finished(job)['state'], 'completed')
        self.assertEqual(target.read_text(), 'new')
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_merge_directories_preserves_unrelated_files(self):
        source = self.root / 'source'; source.mkdir(); (source / 'new').write_text('new')
        target = self.root / 'target'; target.mkdir(); (target / 'old').write_text('old')
        job = self.copy(source, target)
        conflict = wait_for(lambda: self.manager.conflicts())[0]
        self.assertEqual(conflict.choices, ('cancel', 'keep_both', 'merge'))
        self.manager.resolve_conflict(job, 'merge')
        self.assertEqual(self.finished(job)['state'], 'completed')
        self.assertEqual((target / 'old').read_text(), 'old')
        self.assertEqual((target / 'new').read_text(), 'new')

    def slow_source(self, source):
        backend = self.workspace.devices['local'].filesystem_backend
        original = backend.transfer_reader
        entered, release = threading.Event(), threading.Event()
        reads = []
        def reader(path, info, control):
            wrapped = original(path, info, control)
            real_read = wrapped.read
            def read(size):
                reads.append(size)
                entered.set()
                while not release.wait(.01):
                    control.check()
                time.sleep(.001)
                return real_read(size)
            wrapped.read = read
            return wrapped
        backend.transfer_reader = reader
        self.addCleanup(release.set)
        return entered, release, reads

    def test_cancellation_cleans_partial_without_publishing(self):
        source = self.root / 'source'; source.write_bytes(b'x' * CHUNK_SIZE * 8)
        entered, release, reads = self.slow_source(source)
        job = self.copy(source, self.root / 'target')
        self.assertTrue(entered.wait(3))
        self.manager.cancel(job)
        self.assertEqual(self.finished(job)['state'], 'cancelled')
        self.assertFalse((self.root / 'target').exists())
        self.assertFalse(list(self.root.glob('.naui-partial-*')))
        self.assertLessEqual(max(reads), CHUNK_SIZE)

    def test_pause_resume_and_retry_are_real_state_transitions(self):
        source = self.root / 'source'; source.write_bytes(b'x' * CHUNK_SIZE * 8)
        entered, release, reads = self.slow_source(source)
        job = self.copy(source, self.root / 'target')
        self.assertTrue(entered.wait(3))
        self.manager.pause(job)
        release.set(); time.sleep(.1)
        self.assertEqual(self.manager.status(job)['state'], 'paused')
        self.assertEqual(self.manager.status(job)['bytes_transferred'], 0)
        self.manager.resume(job)
        self.assertEqual(self.finished(job)['state'], 'completed')
        with self.assertRaises(ValueError):
            self.manager.retry(job)
        missing = self.copy(self.root / 'missing', self.root / 'created')
        self.assertEqual(self.finished(missing)['state'], 'failed')
        (self.root / 'missing').write_text('now available')
        retry = self.manager.retry(missing)
        self.assertEqual(self.finished(retry)['state'], 'completed')

    def test_destination_race_and_changed_source_do_not_publish(self):
        source = self.root / 'source'; source.write_bytes(b'x' * CHUNK_SIZE * 2)
        entered, release, _ = self.slow_source(source)
        job = self.copy(source, self.root / 'target')
        self.assertTrue(entered.wait(3))
        (self.root / 'target').write_text('another writer')
        release.set()
        self.assertEqual(self.finished(job)['state'], 'failed')
        self.assertEqual((self.root / 'target').read_text(), 'another writer')
        self.assertFalse(list(self.root.glob('.naui-partial-*')))
        entered.clear(); release.clear()
        job = self.copy(source, self.root / 'second')
        self.assertTrue(entered.wait(3))
        source.write_bytes(b'z' * source.stat().st_size)
        release.set()
        self.assertEqual(self.finished(job)['state'], 'failed')
        self.assertFalse((self.root / 'second').exists())

    def test_unsafe_locations_self_copy_and_move_are_rejected(self):
        with self.assertRaises(ValueError):
            FileLocation('local', 'relative')
        source = self.root / 'folder'; source.mkdir()
        alias = self.root / 'alias'; alias.symlink_to(source, target_is_directory=True)
        for target in (source, source / 'child', alias / 'child'):
            with self.assertRaises(ValueError):
                self.copy(source, target)
        with self.assertRaises(NotImplementedError):
            self.manager.move(None, None)

    def test_all_four_directions_and_remote_to_remote_bounded_streams(self):
        a, a_root = self.remote('server-a'); b, b_root = self.remote('server-b')
        local = self.root / 'payload'; local.write_bytes(bytes(range(256)) * 12000)
        jobs = [self.copy(local, self.root / 'local-copy'),
                self.copy(local, '/remote-source', target_id=a.id)]
        for job in jobs:
            self.assertEqual(self.finished(job)['state'], 'completed', self.manager.status(job))
        job = self.copy('/remote-source', self.root / 'download', source_id=a.id)
        self.assertEqual(self.finished(job)['state'], 'completed', self.manager.status(job))
        self.assertEqual((self.root / 'download').read_bytes(), local.read_bytes())
        sizes = []
        real_reader = a.filesystem_backend.transfer_reader
        def instrumented(path, info, control):
            stream = real_reader(path, info, control)
            real_read = stream.read
            stream.read = lambda size: (sizes.append(size), real_read(size))[1]
            return stream
        a.filesystem_backend.transfer_reader = instrumented
        job = self.copy('/remote-source', '/copy õ $(touch BAD)', source_id=a.id, target_id=b.id)
        result = self.finished(job)
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual((b_root / 'copy õ $(touch BAD)').read_bytes(), local.read_bytes())
        self.assertLessEqual(max(sizes), CHUNK_SIZE)
        self.assertFalse((b_root / 'BAD').exists())
        self.assertTrue((a_root / 'remote-source').exists())
        self.assertFalse(list(b_root.glob('.naui-partial-*')))
        self.assertFalse(a.filesystem_backend._processes)
        self.assertFalse(b.filesystem_backend._processes)

    def test_remote_folder_and_remote_keep_both(self):
        a, a_root = self.remote('server-a'); b, b_root = self.remote('server-b')
        source = a_root / 'folder'; (source / 'sub').mkdir(parents=True)
        (source / 'sub/file').write_text('remote folder õ')
        (source / 'loop').symlink_to(source, target_is_directory=True)
        (b_root / 'folder').mkdir()
        job = self.copy('/folder', '/folder', source_id=a.id, target_id=b.id)
        wait_for(lambda: self.manager.conflicts())
        self.manager.resolve_conflict(job, 'keep_both')
        result = self.finished(job)
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual((b_root / 'folder (1)/sub/file').read_text(), 'remote folder õ')
        self.assertEqual(result['skipped'], 1)

    def test_remote_cancellation_no_commit_leaves_old_destination(self):
        a, a_root = self.remote('server-a')
        source = self.root / 'source'; source.write_bytes(b'x' * CHUNK_SIZE * 16)
        (a_root / 'destination').write_text('old')
        entered, release, _ = self.slow_source(source)
        job = self.copy(source, '/destination', target_id=a.id)
        wait_for(lambda: self.manager.conflicts())
        self.manager.resolve_conflict(job, 'replace')
        self.assertTrue(entered.wait(3))
        self.manager.cancel_device(a.id)
        self.assertEqual(self.finished(job)['state'], 'cancelled')
        self.assertEqual((a_root / 'destination').read_text(), 'old')
        self.assertFalse(list(a_root.glob('.naui-partial-*')))

    def test_shutdown_cancels_jobs_queued_running_and_waiting_conflict(self):
        source = self.root / 'source'; source.write_text('new')
        target = self.root / 'target'; target.write_text('old')
        jobs = [self.copy(source, target) for _ in range(6)]
        wait_for(lambda: self.manager.conflicts())
        self.manager.close(); self.manager.close(); self.manager.join()
        self.assertTrue(all(self.manager.status(job)['state'] == 'cancelled' for job in jobs))
        self.assertEqual(self.manager.active(), 0)
        self.assertEqual(target.read_text(), 'old')
        with self.assertRaises(RuntimeError):
            self.copy(source, self.root / 'other')

    def test_waiting_auth_and_device_generation_guards(self):
        source = self.root / 'source'; source.write_text('new')
        entered, release, _ = self.slow_source(source)
        backend = self.workspace.devices['local'].filesystem_backend
        backend.generation = 1
        job = self.copy(source, self.root / 'target')
        self.assertTrue(entered.wait(3))
        self.manager.controls[job].waiting_auth(True)
        self.assertEqual(self.manager.status(job)['state'], 'waiting_auth')
        self.manager.controls[job].waiting_auth(False)
        backend.generation = 2
        release.set()
        self.assertEqual(self.finished(job)['state'], 'failed')
        self.assertFalse((self.root / 'target').exists())

    def test_clear_finished_does_not_cancel_active_conflicts(self):
        source = self.root / 'source'; source.write_text('new')
        completed = self.copy(source, self.root / 'copied')
        self.assertEqual(self.finished(completed)['state'], 'completed')
        (self.root / 'conflict').write_text('old')
        active = self.copy(source, self.root / 'conflict')
        wait_for(lambda: self.manager.conflicts())
        self.manager.clear_finished()
        self.assertNotIn(completed, self.manager.jobs)
        self.assertIn(active, self.manager.jobs)
        self.assertEqual(self.manager.active(), 1)
        self.manager.cancel(active)
        self.assertEqual(self.finished(active)['state'], 'cancelled')

    def test_same_ssh_identity_cannot_copy_folder_into_itself_even_under_another_device_id(self):
        a, a_root = self.remote('server-a')
        duplicate = self.workspace.add_ssh('server-a')
        duplicate.filesystem_backend = a.filesystem_backend
        with self.assertRaises(ValueError):
            self.copy('/folder', '/folder/child', source_id=a.id, target_id=duplicate.id)

    def test_directory_identity_is_checked_before_progressive_enumeration(self):
        from naui.backends import ConflictError
        source = self.root / 'source'; source.mkdir()
        (source / 'initial').write_text('initial')
        backend = self.workspace.devices['local'].filesystem_backend
        info = backend.properties(str(source))
        (source / 'later').write_text('later')
        control = SimpleNamespace(check=lambda: None)
        iterator = backend.iter_directory(str(source), control, info)
        with self.assertRaises(ConflictError):
            next(iterator)

    def test_metadata_authentication_receives_control_and_cancels_its_owned_ssh(self):
        from contextlib import contextmanager
        import sys
        from naui.ssh import ssh_environment
        device, directory = self.remote('auth-target')
        entered = threading.Event()
        received = []
        class Authentication:
            @contextmanager
            def operation(self, identity, control):
                received.append((identity, control))
                control.waiting_auth(True)
                entered.set()
                try:
                    yield ssh_environment()
                finally:
                    control.waiting_auth(False)
        class SlowSsh:
            host, username, options = 'auth-target', None, ()
            def command(self, script, *, batch=False):
                return [sys.executable, '-c', 'import time;time.sleep(20)']
        backend = device.filesystem_backend
        backend.authentication, backend.device_id = Authentication(), device.id
        backend.connection = SlowSsh()
        job = self.copy('/file', self.root / 'destination', source_id=device.id)
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.manager.status(job)['state'], 'waiting_auth')
        self.assertEqual(received[0][0], device.id)
        self.assertIs(received[0][1], self.manager.controls[job])
        self.manager.cancel(job)
        self.assertEqual(self.finished(job)['state'], 'cancelled')
        self.assertFalse(backend._processes)
        self.assertFalse((self.root / 'destination').exists())

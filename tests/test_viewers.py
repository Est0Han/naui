import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from naui import __version__
from naui.backends import FileError, LocalFileBackend, RemoteFileBackend
from naui.metadata import project_url, version_string
from naui.preview import PreviewCache, PreviewSource
from naui.viewer_registry import (DEFAULT_VIEWERS, ViewerRegistry, format_time, image_geometry,
                                  looks_like_text, sniff_mime)
from media_assets import make_assets
from test_backends import LocalTransport


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = ViewerRegistry()

    def test_all_required_extensions_have_a_viewer(self):
        for spec in DEFAULT_VIEWERS:
            for suffix in spec.extensions:
                with self.subTest(suffix=suffix):
                    self.assertEqual(self.registry.select("file" + suffix.upper()).kind, spec.kind)

    def test_content_signature_and_mime_precede_misleading_extensions(self):
        self.assertEqual(self.registry.select("photo.py", b"\x89PNG\r\n\x1a\n").kind, "image")
        self.assertEqual(self.registry.select("song.txt", b"RIFF\0\0\0\0WAVE").kind, "audio")
        self.assertEqual(self.registry.select("wrong.jpg", mime="video/mp4").kind, "video")
        self.assertEqual(self.registry.select("unknown.data", mime="application/json").kind, "text")
        self.assertEqual(self.registry.select("file.txt", mime="application/pdf").kind, "unsupported")

    def test_unknown_binary_cannot_fall_through_to_text(self):
        self.assertEqual(self.registry.select("binary.blob", b"\0\xff\xfe").kind, "unsupported")
        self.assertEqual(self.registry.select("README", "Unicode õ\n".encode()).kind, "text")
        self.assertTrue(looks_like_text(b"partial utf8 \xc3"))
        self.assertEqual(self.registry.select("a.pdf", b"%PDF-1.5\n").kind, "unsupported")

    def test_renamed_playlist_stays_text_and_is_not_played(self):
        self.assertEqual(self.registry.select("video.mp4", b"#EXTM3U\nhttps://example.invalid/video\n").kind, "text")

    def test_formats_and_geometry_time_helpers(self):
        for data, mime in [(b"GIF89a\0", "image/gif"), (b"RIFFxxxxWEBP", "image/webp"),
                           (b"fLaC", "audio/flac"), (b"ID3", "audio/mpeg"),
                           (b"\0\0\0\x20ftypM4A ", "audio/mp4"),
                           (b"\x1aE\xdf\xa3webm", "video/webm")]:
            self.assertEqual(sniff_mime(data), mime)
        self.assertEqual(image_geometry(200, 100, 100, 100), (0, 25, 100, 50))
        self.assertEqual(image_geometry(200, 100, 100, 100, 1), (-50, 0, 200, 100))
        self.assertEqual(image_geometry(0, 100, 100, 100), (0, 0, 0, 0))
        self.assertEqual(format_time(65_000_000), "1:05")
        self.assertEqual(format_time(3601_000_000), "1:00:01")


class PreviewBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.backend = LocalFileBackend()
        self.remote = RemoteFileBackend(LocalTransport())
        self.addCleanup(self.remote.close)

    def test_probe_binary_without_utf8_decode_and_no_source_changes(self):
        path = self.root / "space ';$(touch NEVER_EXECUTED) õ\n.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff\0" * 10000)
        before = path.stat()
        for backend in (self.backend, self.remote):
            probe = backend.probe(str(path))
            self.assertEqual(probe.sample, path.read_bytes()[:8192])
            self.assertEqual(ViewerRegistry().select(str(path), probe.sample).kind, "image")
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(Path("NEVER_EXECUTED").exists())

    def test_cache_remote_permissions_release_and_cleanup(self):
        path = self.root / "data.png"
        path.write_bytes(b"\0\xffsource image bytes")
        cache = PreviewCache(self.root / "cache", limit=1024)
        source = cache.prepare(self.remote, str(path), remote=True)
        local = Path(source.local_path)
        self.assertTrue(source.cached)
        self.assertEqual(local.read_bytes(), path.read_bytes())
        self.assertEqual(local.stat().st_mode & 0o777, 0o600)
        self.assertEqual(local.parent.stat().st_mode & 0o777, 0o700)
        cache.release(source)
        cache.release(source)
        self.assertFalse(local.exists())
        pending = cache.prepare(self.remote, str(path), remote=True)
        cache.close()
        cache.close()
        self.assertFalse(Path(pending.local_path).exists())
        self.assertEqual(path.read_bytes(), b"\0\xffsource image bytes")

    def test_local_preview_never_deletes_original_and_cache_rejects_other_paths(self):
        path = self.root / "local.png"
        path.write_bytes(b"original")
        cache = PreviewCache(self.root / "cache")
        self.addCleanup(cache.close)
        local = cache.prepare(self.backend, str(path))
        cache.release(local)
        self.assertTrue(path.exists())
        remote = cache.prepare(self.remote, str(path), True)
        with self.assertRaises(ValueError):
            cache.release(PreviewSource(str(path), True))
        cache.release(remote)

    def test_remote_download_limit_and_failed_cache_are_cleaned(self):
        path = self.root / "large.mp4"
        path.write_bytes(b"x" * 100)
        cache = PreviewCache(self.root / "cache", limit=20)
        self.addCleanup(cache.close)
        with self.assertRaises(FileError):
            cache.prepare(self.remote, str(path), True)
        self.assertEqual(list(Path(cache._directory.name).glob('media-*')), [])
        self.assertEqual(path.stat().st_size, 100)
        cache.cancel()
        with self.assertRaises(OSError):
            cache.prepare(self.remote, str(path), True)

    def test_relative_xdg_cache_is_ignored_instead_of_using_project_directory(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": "relative-project-directory"}):
            cache = PreviewCache()
            self.assertEqual(cache.base, Path.home() / ".cache/naui")
            self.assertIsNone(cache._directory)
            cache.close()

    def test_locally_enforced_cap_when_remote_ignores_requested_script(self):
        class UntrustedTransport:
            host = "test"
            def command(self, script, *, batch=False):
                return [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*100000)"]
        backend = RemoteFileBackend(UntrustedTransport())
        self.addCleanup(backend.close)
        with self.assertRaises(FileError):
            backend.probe("/arbitrary.mp4")
        self.assertFalse(backend._processes)
        cache = PreviewCache(self.root / "cache", limit=100)
        self.addCleanup(cache.close)
        with self.assertRaises(FileError):
            cache.prepare(backend, "/arbitrary.mp4", True)
        self.assertEqual(list(Path(cache._directory.name).glob('media-*')), [])
        self.assertFalse(backend._processes)

    def test_inflight_preview_transfer_cancelled_before_cache_cleanup(self):
        class BlockingTransport:
            host = "test"
            def command(self, script, *, batch=False):
                return [sys.executable, "-c", "import time; time.sleep(60)"]
        backend = RemoteFileBackend(BlockingTransport())
        cache = PreviewCache(self.root / "cache")
        failures = []
        def download():
            try:
                cache.prepare(backend, "/pending.mp4", True)
            except OSError as error:
                failures.append(error)
        thread = threading.Thread(target=download)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not backend._processes and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(backend._processes)
            with backend._lock:
                processes = list(backend._processes)
            cache.cancel()
            backend.close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertTrue(all(process.returncode is not None for process in processes))
            cache.close()
            self.assertFalse(list((self.root / "cache").glob("preview-*")))
        finally:
            cache.cancel()
            backend.close()
            thread.join(3)
            cache.close()

    def test_fifo_and_directory_are_not_opened_by_codecs(self):
        fifo = self.root / "fifo.png"
        os.mkfifo(fifo)
        for path in (fifo, self.root):
            with self.assertRaises(FileError):
                self.backend.probe(str(path))
            with self.assertRaises(FileError):
                self.remote.probe(str(path))

    def test_generated_images_and_audio_have_correct_registry_types(self):
        assets = make_assets(self.root, codecs=True)
        for name, kind in [("pixels.png", "image"), ("pixels.jpg", "image"), ("tone.wav", "audio"),
                           ("tone.mp3", "audio"), ("clip.mp4", "video")]:
            path = assets / name
            if path.exists():  # Compressed fixture codecs are optional test tools.
                self.assertEqual(ViewerRegistry().select(str(path), self.backend.probe(str(path)).sample).kind, kind)
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf, GLib
        except (ImportError, ValueError):
            return
        try:
            pixels = GdkPixbuf.Pixbuf.new_from_file(str(assets / "pixels.png"))
        except GLib.Error as error:
            if "Operation not permitted" in str(error):
                self.skipTest("System Glycin image decoder's D-Bus/socket access is blocked in this sandbox")
            raise
        self.assertEqual((pixels.get_width(), pixels.get_height()), (16, 12))


class VersionTests(unittest.TestCase):
    def test_cli_and_build_metadata_share_authoritative_version(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([str(root / "naui"), "--version"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), version_string())
        self.assertEqual(version_string(), "Naui " + __version__)
        config = (root / "pyproject.toml").read_text()
        self.assertIn('version = {attr = "naui.__version__"}', config)
        self.assertNotIn('version = "0.', config)

    def test_project_url_is_configurable_and_no_fake_default(self):
        with patch.dict(os.environ, {"NAUI_PROJECT_URL": ""}):
            self.assertEqual(project_url(), "")
        with patch.dict(os.environ, {"NAUI_PROJECT_URL": "https://example.org/naui"}):
            self.assertEqual(project_url(), "https://example.org/naui")
        with patch.dict(os.environ, {"NAUI_PROJECT_URL": "javascript:alert(1)"}):
            self.assertEqual(project_url(), "")

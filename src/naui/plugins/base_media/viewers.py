"""GTK preview tabs. File transfers are plain-data jobs; codecs stay on GTK.

Players use GTK's installed media implementation (normally GStreamer). There
are no bundled codecs, external web pages, shell launches or automatic playback.
"""
import math
from pathlib import Path

import gi
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Graphene, Gtk

from ...backends import DisconnectedError
from ...lifecycle import Signals
from ...viewer_registry import format_time, image_geometry

MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000


class PreviewViewer(Gtk.Box):
    dirty = saving = False

    def __init__(self, window, backend, path, session=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.window, self.backend, self.path, self.session = window, backend, path, session
        device = window.workspace.device_for_backend(backend)
        self.device_id = device.id if device else None
        self.remote = bool(device and device.type == 'ssh') or hasattr(backend, 'connection')
        self.cache = window.preview_cache
        self.signals = Signals()
        self._shutting_down = False
        self.loading, self.disconnected = True, False
        self.source = None
        self.label = Gtk.Label()
        self.filename = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.filename.set_text(("REMOTE — " + backend.connection.host + "\n" if self.remote else "LOCAL\n") + path)
        self.filename.set_margin_start(8)
        self.append(self.filename)
        self.status = Gtk.Label(label="Loading preview…", xalign=0, wrap=True, selectable=True)
        self.status.set_margin_start(8)
        self.append(self.status)
        self.update_label()

    def update_label(self):
        if self._shutting_down:
            return
        title = Path(self.path).name or self.path
        if self.remote:
            device = self.window.workspace.devices.get(self.device_id)
            title += " — " + (device.display_name if device else self.backend.connection.host)
        if self.disconnected:
            title += " [disconnected]"
        elif self.loading:
            title += " [loading]"
        self.label.set_text(title)
        self.label.set_tooltip_text(self.filename.get_text())

    def start(self):
        cache, backend, path, remote = self.cache, self.backend, self.path, self.remote
        generation = getattr(backend, "generation", 0)

        def ready(source, error):
            if self._shutting_down or self.window._shutting_down:
                cache.release(source)
                return
            self.loading = False
            if error:
                self.status.set_text(str(error))
                if isinstance(error, DisconnectedError) and generation == getattr(backend, "generation", 0):
                    self.window.disconnect_session(self.session, str(error))
            else:
                self.source = source
                try:
                    self.load(source.local_path)
                except (OSError, ValueError, GLib.Error) as error:
                    # GI exposes native image/codec errors as GLib.Error, not
                    # OSError. Report the decoder failure; leave the tab open.
                    self.status.set_text("Cannot preview this file: " + self.display_error(error))
                if remote and not backend.connected:
                    self.mark_disconnected()
            self.update_label()
        self.window.work(lambda: cache.prepare(backend, path, remote), ready)

    def display_error(self, error):
        message = str(error)
        if self.remote and self.source:
            physical = self.source.local_path
            message = message.replace(Path(physical).as_uri(), self.path).replace(physical, self.path)
        return message

    def mark_disconnected(self, reason=None):
        if self._shutting_down:
            return
        self.disconnected = True
        self.update_label()
        self.status.set_text(reason or "SSH disconnected. Cached previews remain available; mounted previews require reconnection.")

    def close(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.signals.close()
        self.stop()
        self.cache.release(self.source)
        self.source = None
        self.window = None

    def stop(self):
        pass


class ImageCanvas(Gtk.Widget):
    """Texture snapshot with explicit measurements for fit and scrollable zoom."""
    def __init__(self):
        super().__init__()
        self.texture, self.zoom = None, None
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_measure(self, orientation, for_size):
        if self.texture is None or self.zoom is None:
            return (1, 480 if orientation == Gtk.Orientation.HORIZONTAL else 320, -1, -1)
        length = self.texture.get_width() if orientation == Gtk.Orientation.HORIZONTAL else self.texture.get_height()
        size = max(1, math.ceil(length * self.zoom))
        return (size, size, -1, -1)

    def do_snapshot(self, snapshot):
        if self.texture is None:
            return
        x, y, width, height = image_geometry(self.texture.get_width(), self.texture.get_height(),
                                            self.get_width(), self.get_height(), self.zoom)
        bounds = Graphene.Rect()
        bounds.init(x, y, width, height)
        snapshot.append_texture(self.texture, bounds)


class ImageViewer(PreviewViewer):
    def __init__(self, *args):
        super().__init__(*args)
        self.original, self.rotation = None, 0
        self.canvas = ImageCanvas()
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_vexpand(True)
        self.scroll.set_child(self.canvas)
        self.toolbar = Gtk.Box(spacing=6)
        for label, action in (("Fit", lambda: self.set_zoom(None)), ("100%", lambda: self.set_zoom(1)),
                              ("−", lambda: self.zoom_by(1 / 1.25)), ("+", lambda: self.zoom_by(1.25)),
                              ("Rotate ↶", lambda: self.rotate(-1)), ("Rotate ↷", lambda: self.rotate(1))):
            button = Gtk.Button(label=label)
            self.signals.connect(button, "clicked", lambda button, callback=action: callback())
            self.toolbar.append(button)
        self.toolbar.set_sensitive(False)
        self.append(self.toolbar)
        self.append(self.scroll)
        self.set_zoom(None)

    def load(self, path):
        if Path(path).stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 128 MiB preview limit")
        info, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
        if info is None or min(width, height) <= 0:
            raise ValueError("Unsupported or invalid image; the system image loader may be missing")
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds the 32 megapixel preview limit")
        self.original = GdkPixbuf.Pixbuf.new_from_file(path)
        self.rotation = 0
        self._texture()
        self.toolbar.set_sensitive(True)

    def _texture(self):
        rotations = (GdkPixbuf.PixbufRotation.NONE, GdkPixbuf.PixbufRotation.CLOCKWISE,
                     GdkPixbuf.PixbufRotation.UPSIDEDOWN, GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE)
        pixels = self.original if self.rotation == 0 else self.original.rotate_simple(rotations[self.rotation])
        self.canvas.texture = Gdk.Texture.new_for_pixbuf(pixels)
        self.canvas.queue_resize()
        self.canvas.queue_draw()
        self._status()

    def _status(self):
        if self.original:
            zoom = "Fit" if self.canvas.zoom is None else f"{self.canvas.zoom * 100:.0f}%"
            self.status.set_text(f"{self.original.get_width()} × {self.original.get_height()} pixels — {zoom}"
                                 + (f" — view rotated {self.rotation * 90}°" if self.rotation else ""))

    def set_zoom(self, zoom):
        if self._shutting_down:
            return
        self.canvas.zoom = None if zoom is None else max(0.05, min(8.0, zoom))
        policy = Gtk.PolicyType.NEVER if zoom is None else Gtk.PolicyType.AUTOMATIC
        self.scroll.set_policy(policy, policy)
        self.canvas.queue_resize()
        self.canvas.queue_draw()
        self._status()

    def zoom_by(self, factor):
        if self._shutting_down or self.canvas.texture is None:
            return
        zoom = self.canvas.zoom
        if zoom is None:
            zoom = min(max(1, self.canvas.get_width()) / self.canvas.texture.get_width(),
                       max(1, self.canvas.get_height()) / self.canvas.texture.get_height())
        self.set_zoom(zoom * factor)

    def rotate(self, direction):
        if not self._shutting_down and self.original:
            self.rotation = (self.rotation + direction) % 4
            self._texture()

    def stop(self):
        self.canvas.texture = None
        self.original = None


class MediaViewer(PreviewViewer):
    video = False

    def __init__(self, *args):
        super().__init__(*args)
        self.media = None
        self._updating_seek = False
        self.video_widget = Gtk.Video() if self.video else None
        if self.video_widget:
            self.video_widget.set_autoplay(False)
            self.video_widget.set_hexpand(True)
            self.video_widget.set_vexpand(True)
            self.append(self.video_widget)
        else:
            art = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
            art.set_pixel_size(96)
            art.set_vexpand(True)
            self.append(art)
        controls = Gtk.Box(spacing=8)
        self.play_button, self.pause_button = Gtk.Button(label="Play"), Gtk.Button(label="Pause")
        self.signals.connect(self.play_button, "clicked", lambda *_: self.play())
        self.signals.connect(self.pause_button, "clicked", lambda *_: self.pause())
        controls.append(self.play_button)
        controls.append(self.pause_button)
        self.clock = Gtk.Label(label="0:00 / —")
        controls.append(self.clock)
        controls.append(Gtk.Label(label="Volume"))
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self.volume.set_size_request(140, -1)
        self.volume.set_value(0.8)
        self.volume.set_draw_value(False)
        self.signals.connect(self.volume, "value-changed", self._volume_changed)
        controls.append(self.volume)
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.1)
        self.seek.set_draw_value(False)
        self.seek.set_sensitive(False)
        self.signals.connect(self.seek, "value-changed", self._seek_changed)
        self.append(self.seek)
        self.append(controls)
        self.play_button.set_sensitive(False)
        self.pause_button.set_sensitive(False)

    def load(self, path):
        self.media = Gtk.MediaFile.new()
        for prop in ("prepared", "error", "timestamp", "duration", "playing", "ended", "seekable"):
            self.signals.connect(self.media, "notify::" + prop, self._updated)
        self.media.set_volume(self.volume.get_value())
        if self.video_widget:
            self.video_widget.set_media_stream(self.media)
        # Always a verified LOCAL file, never a user-entered URL or playlist.
        self.media.set_file(Gio.File.new_for_path(path))
        self._updated()

    def _updated(self, *_):
        if self._shutting_down or self.media is None:
            return
        error = self.media.get_error()
        ready = self.media.is_prepared() and error is None
        self.play_button.set_sensitive(ready)
        self.pause_button.set_sensitive(ready and self.media.get_playing())
        self.seek.set_sensitive(ready and self.media.is_seekable() and self.media.get_duration() > 0)
        if error:
            self.status.set_text("Playback unavailable: " + self.display_error(error.message)
                                 + ". Check the system GTK/GStreamer media backend and codecs.")
        elif ready:
            self.status.set_text("Playing" if self.media.get_playing() else "Paused — press Play")
        else:
            self.status.set_text("Preparing media…")
        duration, timestamp = self.media.get_duration(), self.media.get_timestamp()
        self.clock.set_text(format_time(timestamp) + " / " + (format_time(duration) if duration else "—"))
        self._updating_seek = True
        try:
            self.seek.set_range(0, max(1, duration / 1_000_000))
            self.seek.set_value(timestamp / 1_000_000)
        finally:
            self._updating_seek = False

    def _seek_changed(self, scale):
        if not self._shutting_down and not self._updating_seek and self.media and self.media.is_seekable():
            self.media.seek(int(scale.get_value() * 1_000_000))

    def _volume_changed(self, scale):
        if not self._shutting_down and self.media:
            self.media.set_volume(scale.get_value())

    def play(self):
        if not self._shutting_down and self.media and self.media.is_prepared() and self.media.get_error() is None:
            if self.media.get_ended() and self.media.is_seekable():
                self.media.seek(0)
            self.media.play()

    def pause(self):
        if not self._shutting_down and self.media:
            self.media.pause()

    def stop(self):
        if self.media:
            self.media.pause()
        if self.video_widget:
            self.video_widget.set_media_stream(None)
        if self.media:
            self.media.clear()  # Stop codec threads and release file BEFORE unlinking cache.
            self.media = None


class AudioViewer(MediaViewer):
    pass


class VideoViewer(MediaViewer):
    video = True


class UnsupportedViewer(PreviewViewer):
    def start(self):
        self.loading = False
        self.status.set_text("No viewer for this file type. Use Open in Editor for UTF-8 text files.")
        self.update_label()

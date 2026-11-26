"""Frontend-independent, bounded MIME detection and viewer selection.

Supported binary signatures take precedence over an extension (a PNG named
notes.txt is still an image). MIME hints precede extension fallback. Unknown
binary data never falls through into an editor or an external executable.
"""
import codecs
from dataclasses import dataclass
import mimetypes
from pathlib import PurePath

PROBE_BYTES = 8192


@dataclass(frozen=True)
class ViewerSpec:
    kind: str
    mime_types: tuple
    extensions: tuple

    def handles(self, mime):
        return any(mime == value or (value.endswith("/*") and mime.startswith(value[:-1]))
                   for value in self.mime_types)


DEFAULT_VIEWERS = (
    ViewerSpec("executable", ("application/x-dosexec", "application/vnd.microsoft.portable-executable"), (".exe", ".dll")),
    ViewerSpec("rpf", ("application/x-rage-rpf",), (".rpf",)),
    ViewerSpec("image", ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/x-ms-bmp"),
               (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")),
    ViewerSpec("video", ("video/mp4", "video/webm", "video/x-matroska", "video/quicktime", "video/ogg"),
               (".mp4", ".webm", ".mkv", ".mov")),
    ViewerSpec("audio", ("audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "application/ogg",
                         "audio/flac", "audio/x-flac", "audio/mp4", "audio/x-m4a"),
               (".mp3", ".wav", ".ogg", ".flac", ".m4a")),
    ViewerSpec("text", ("text/*", "application/json", "application/yaml", "application/x-yaml",
                        "application/toml", "application/x-shellscript"),
               (".txt", ".md", ".py", ".c", ".cpp", ".h", ".hpp", ".json", ".yaml", ".yml",
                ".toml", ".ini", ".conf", ".sh", ".bash", ".zsh", ".css", ".js", ".html", ".xml")),
)
UNSUPPORTED = ViewerSpec("unsupported", (), ())


def sniff_mime(sample, path=""):
    data = sample[:PROBE_BYTES]
    suffix = PurePath(path).suffix.lower()
    signatures = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                  (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"BM", "image/bmp"),
                  (b"fLaC", "audio/flac"), (b"ID3", "audio/mpeg"))
    for magic, mime in signatures:
        if data.startswith(magic):
            return mime
    if data[:4] in (b"RPF7", b"7FPR", b"RPF8", b"8FPR", b"RPF9", b"9FPR"):
        return "application/x-rage-rpf"
    if data.startswith(b"MZ"):
        return "application/vnd.microsoft.portable-executable"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data[:4] == b"RIFF":
        return {b"WEBP": "image/webp", b"WAVE": "audio/wav"}.get(data[8:12])
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4" if data[8:12] in {b"M4A ", b"M4B "} or suffix == ".m4a" else "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm" if b"webm" in data[:512] or suffix == ".webm" else "video/x-matroska"
    if data.startswith(b"OggS"):
        return "video/ogg" if b"theora" in data[:128] else "audio/ogg"
    if len(data) >= 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0:
        return "audio/mpeg"
    return None


def looks_like_text(sample):
    sample = sample[:PROBE_BYTES]
    if b"\0" in sample:
        return False
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeError:
        return False
    return not any(ord(character) < 32 and character not in "\n\r\t\f" for character in text)


class ViewerRegistry:
    def __init__(self, viewers=DEFAULT_VIEWERS):
        self.viewers = tuple(viewers)

    def select(self, path, sample=None, mime=None):
        detected = sniff_mime(sample, path) if sample is not None else None
        detected = detected or mime
        if detected and detected != "application/octet-stream":
            for viewer in self.viewers:
                if viewer.handles(detected):
                    return viewer
            return UNSUPPORTED
        if sample and looks_like_text(sample):
            # A text playlist renamed .mp4 remains text; do not hand it to a
            # decoder that could follow its external/network references.
            return next(viewer for viewer in self.viewers if viewer.kind == "text")
        extension = PurePath(path).suffix.lower()
        for viewer in self.viewers:
            if extension in viewer.extensions:
                return viewer
        guessed = mimetypes.guess_type(path)[0]
        if guessed:
            for viewer in self.viewers:
                if viewer.handles(guessed):
                    return viewer
            return UNSUPPORTED
        if sample is not None and looks_like_text(sample):
            return next(viewer for viewer in self.viewers if viewer.kind == "text")
        return UNSUPPORTED


def image_geometry(width, height, available_width, available_height, zoom=None):
    if min(width, height, available_width, available_height) <= 0:
        return (0, 0, 0, 0)
    scale = min(available_width / width, available_height / height) if zoom is None else zoom
    drawn_width, drawn_height = width * scale, height * scale
    return ((available_width - drawn_width) / 2, (available_height - drawn_height) / 2,
            drawn_width, drawn_height)


def format_time(microseconds):
    seconds = max(0, int(microseconds) // 1_000_000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

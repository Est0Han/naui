"""Generate tiny original test pixels/tones; no downloads or committed media."""
import math
from pathlib import Path
import shutil
import struct
import subprocess
import wave
import zlib


def make_assets(directory, codecs=False):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    width, height = 16, 12
    pixels = b"".join(b"\0" + bytes((x * 16, y * 20, 80)) * width for y in range(height) for x in [1])
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
    (root / "pixels.png").write_bytes(png)
    pcm = b"".join(struct.pack("<h", int(math.sin(i * 2 * math.pi * 440 / 8000) * 1000)) for i in range(8000))
    with wave.open(str(root / "tone.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(pcm)
    # ffmpeg is optional TEST tooling, never a production Naui dependency.
    if shutil.which("ffmpeg"):
        commands = [("pixels.jpg", ["-i", str(root / "pixels.png"), "-frames:v", "1", "-codec:v", "mjpeg"])]
        if codecs:
            commands.extend((
            ("tone.mp3", ["-i", str(root / "tone.wav"), "-codec:a", "libmp3lame"]),
            ("clip.mp4", ["-f", "lavfi", "-i", "color=c=blue:s=32x32:d=1", "-codec:v", "mpeg4", "-an"]),
            ))
        for name, arguments in commands:
            result = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *arguments,
                                     str(root / name)], capture_output=True, timeout=20)
            if result.returncode:
                (root / name).unlink(missing_ok=True)
    return root

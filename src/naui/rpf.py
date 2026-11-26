"""Optional OpenRAGE CLI adapter. Never writes to a game archive."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import threading
import time

MAX_OUTPUT = 16 * 1024 * 1024
MAX_ENTRY = 128 * 1024 * 1024


def helper_path():
    return shutil.which('openrage') or str(Path.home() / '.local/share/naui-tools/openrage/openrage')


def child_path(parent, name):
    # Listings are untrusted archive data, not host paths or CLI switches.
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or name in ('.', '..') or '\0' in name:
        raise ValueError('Unsafe archive entry name')
    return parent + '/' + name


def hex_preview(path):
    with open(path, 'rb') as stream:
        data = stream.read(4096)
    return '\n'.join(f'{i:08x}  {data[i:i+16].hex(" "):<47}  ' +
                     ''.join(chr(c) if 32 <= c < 127 else '.' for c in data[i:i+16])
                     for i in range(0, len(data), 16))


class ArchiveReader:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.cancelled = threading.Event()

    def close(self):
        self.cancelled.set()

    def run(self, *args):
        if self.cancelled.is_set():
            raise ValueError('Archive operation cancelled')
        command = [helper_path(), '--game', str(self.path.parent), *args]
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen(command, stdout=output, stderr=errors, stdin=subprocess.DEVNULL)
            except FileNotFoundError as exc:
                raise ValueError('Install the optional OpenRAGE CLI to browse archive contents. See docs/rpf-support.md.') from exc
            deadline = time.monotonic() + 120
            try:
                while process.poll() is None:
                    if self.cancelled.wait(.1):
                        raise ValueError('Archive operation cancelled')
                    if time.monotonic() > deadline:
                        raise ValueError('Archive scan exceeded two minutes. Large recovery scans may require OpenRAGE directly.')
                    if os.fstat(output.fileno()).st_size > MAX_OUTPUT or os.fstat(errors.fileno()).st_size > MAX_OUTPUT:
                        raise ValueError('Archive helper output exceeded the display limit')
                output.seek(0); errors.seek(0)
                result = output.read(MAX_OUTPUT + 1)
                if len(result) > MAX_OUTPUT:
                    raise ValueError('Archive listing is too large')
                if process.returncode:
                    raise ValueError(errors.read(4096).decode('utf-8', 'replace') or 'OpenRAGE could not read this entry')
                return result
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def listing(self, path):
        rows = json.loads(self.run('--json', 'ls', '--', path))
        if not isinstance(rows, list):
            raise ValueError('Invalid archive listing')
        for row in rows:
            child_path(path, row['path'])
        return rows

    def extract(self, path, *, xml=False):
        # Each operation gets a persistent, unique directory: edits survive tab closure
        # and extraction can never overwrite an existing edited copy.
        root = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'naui/extracted'
        root.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix='rpf-', dir=root))
        if xml:
            result = self.run('xml', '--', path)
            destination = target / (PurePosixPath(path).name + '.xml')
            destination.write_bytes(result)
        else:
            self.run('extract', '--out', str(target), '--', path)
            files = list(target.rglob('*'))
            if any(p.is_symlink() for p in files):
                raise ValueError('Extracted symbolic links are not supported')
            files = [p for p in files if p.is_file()]
            if len(files) != 1:
                raise ValueError('Expected one extracted file')
            destination = files[0]
        if destination.stat().st_size > MAX_ENTRY:
            raise ValueError('Extracted file exceeds the 128 MiB preview limit')
        return str(destination)

    def readable(self, path):
        """Decode known formats; never present a strings dump as decompiled code."""
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in {'.psin', '.psch', '.pso', '.ymap', '.ytyp', '.ymt', '.ymf', '.cut',
                      '.#mt', '.#typ', '.#ldbmt', '.#map'}:
            return self.extract(path, xml=True)
        if suffix in {'.gxt2', '.nametable', '.gxt', '.wpl', '.ysc'}:
            command = 'script' if suffix == '.ysc' else 'text'
            content = self.run(command, '--', path)
            raw = Path(self.extract(path))
            target = raw.with_name(raw.name + ('.disassembly.txt' if command == 'script' else '.txt'))
            target.write_bytes(content)
            return str(target)
        raw = Path(self.extract(path))
        # Validate the complete bounded file, not just its prefix, before opening
        # it as editable UTF-8 text. Binary inspection is capped independently.
        import re
        data = raw.read_bytes()
        try:
            decoded = data.decode('utf-8-sig')
            if not any(ord(c) < 32 and c not in '\n\r\t\f' for c in decoded):
                return str(raw)
        except UnicodeError:
            pass
        sample = data[:1024 * 1024]
        strings = []
        for match in re.finditer(rb'[\x20-\x7e]{6,}', sample):
            strings.append(f'0x{match.start():08x}  {match.group()[:512].decode("ascii")}')
            if len(strings) >= 1000:
                break
        report = raw.with_name(raw.name + '.inspection.txt')
        report.write_text(
            'BINARY INSPECTION — NOT SOURCE CODE\n\n'
            f'Archive entry: {path}\nExtracted binary: {raw}\nSize: {len(data)} bytes\n\n'
            + ('GTA VI .#script decoding is not supported by this reader.\n' if suffix == '.#script' else
               'No readable-code decoder is available for this entry type.\n')
            + 'The original source cannot be recovered by treating binary bytes as text.\n'
            'The excerpts below are inspection aids, not a complete translation of the file.\n'
            'Editing this report does not modify the binary.\n\n'
            'Printable strings (first 1 MiB; at most 1000 strings, 512 characters each):\n'
            + ('\n'.join(strings) or '(No printable strings found.)')
            + '\n\nFirst 4096 bytes in hexadecimal:\n' + hex_preview(raw) + '\n', encoding='utf-8')
        return str(report)

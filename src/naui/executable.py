"""Bounded PE inspection based on Microsoft's PE/COFF specification.

Reads bytes only: never loads the image or invokes an executable/disassembler.
"""
import os
import re
import struct

STRING_SCAN_LIMIT = 8 * 1024 * 1024


def inspect_executable(path):
    with open(path, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        def read(offset, count):
            if offset < 0 or offset + count > size:
                raise ValueError('Truncated or invalid executable header')
            stream.seek(offset)
            data = stream.read(count)
            if len(data) != count:
                raise ValueError('File changed while reading')
            return data
        summary = ['Read-only inspection — the file has not been executed.',
                   'This is compiled binary data, not recovered source code.', f'File size: {size:,} bytes']
        sections = []
        try:
            dos = read(0, 64)
            if dos[:2] != b'MZ':
                raise ValueError('Not a Windows MZ/PE executable')
            offset = struct.unpack_from('<I', dos, 60)[0]
            header = read(offset, 24)
            if header[:4] != b'PE\0\0':
                raise ValueError('MZ image without a PE header (legacy or unsupported format)')
            machine, count, _, _, _, optional_size, flags = struct.unpack_from('<HHIIIHH', header, 4)
            if count > 96 or optional_size > 4096:
                raise ValueError('Unsupported section count or optional-header size')
            optional = read(offset + 24, optional_size)
            if len(optional) < 2:
                raise ValueError('Missing PE optional header')
            magic = struct.unpack_from('<H', optional)[0]
            if magic not in (0x10b, 0x20b):
                raise ValueError('Unsupported PE optional-header format')
            directory_offset = 112 if magic == 0x20b else 96
            if len(optional) < directory_offset:
                raise ValueError('Truncated PE optional header')
            entry = struct.unpack_from('<I', optional, 16)[0]
            base = struct.unpack_from('<Q' if magic == 0x20b else '<I', optional, 24 if magic == 0x20b else 28)[0]
            directories = struct.unpack_from('<I', optional, directory_offset - 4)[0]
            managed = False
            if directories > 14:
                if len(optional) < directory_offset + 15 * 8:
                    raise ValueError('Truncated PE data directories')
                managed = all(struct.unpack_from('<II', optional, directory_offset + 14 * 8))
            summary += [f'Format: {"PE32+ (64-bit)" if magic == 0x20b else "PE32 (32-bit)"}',
                        f'Type: {"DLL" if flags & 0x2000 else "Executable"}',
                        f'Architecture: { {0x14c:"x86", 0x8664:"x64", 0xaa64:"ARM64", 0x1c4:"ARM Thumb-2"}.get(machine, "Unknown") } (0x{machine:04x})',
                        f'CLR header: {"present (.NET / mixed mode)" if managed else "absent"}',
                        f'Entry point RVA: 0x{entry:x}', f'Image base: 0x{base:x}', f'Sections: {count}']
            table = read(offset + 24 + optional_size, count * 40)
            for index in range(count):
                row = table[index * 40:(index + 1) * 40]
                name = row[:8].split(b'\0')[0].decode('ascii', 'replace')
                virtual_size, rva, raw_size, raw_offset = struct.unpack_from('<IIII', row, 8)
                characteristics = struct.unpack_from('<I', row, 36)[0]
                access = ''.join(letter for flag, letter in ((0x40000000, 'R'), (0x80000000, 'W'), (0x20000000, 'X')) if characteristics & flag)
                warning = ' [outside file]' if raw_size and raw_offset + raw_size > size else ''
                sections.append(f'{name!r:12} RVA 0x{rva:08x}  offset 0x{raw_offset:08x}  disk {raw_size:,}  memory {virtual_size:,}  {access}{warning}')
        except ValueError as error:
            summary.append('Header inspection: ' + str(error))
        stream.seek(0)
        sample = stream.read(STRING_SCAN_LIMIT)
    strings = []
    for encoding, pattern in [('ASCII', rb'[\x20-\x7e]{6,}'), ('UTF-16LE', rb'(?:[\x20-\x7e]\x00){6,}')]:
        for index, match in enumerate(re.finditer(pattern, sample)):
            value = match.group()[:512 if encoding == 'UTF-16LE' else 256].decode('utf-16le' if encoding == 'UTF-16LE' else 'ascii')
            strings.append(f'0x{match.start():08x} [{encoding}] {value}')
            if index >= 999:
                break
    raw = sample[:4096]
    hex_text = '\n'.join(f'{i:08x}  {raw[i:i+16].hex(" "):<47}  ' + ''.join(chr(c) if 32 <= c < 127 else '.' for c in raw[i:i+16]) for i in range(0, len(raw), 16))
    return {'Overview': '\n'.join(summary), 'Sections': '\n'.join(sections) or 'No supported sections available.',
            'Strings': f'First {len(sample):,} of {size:,} bytes scanned. Up to 1000 ASCII and 1000 UTF-16LE strings, 256 characters each.\nThese are embedded strings, not source code.\n\n' + ('\n'.join(strings) or 'No printable strings found.'),
            'Raw bytes': 'First 4096 bytes (or the complete file if smaller).\n\n' + hex_text}

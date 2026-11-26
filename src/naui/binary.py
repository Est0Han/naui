"""Bounded, read-only inspection of generic binary files."""
import codecs
import os
import re

SCAN_LIMIT = 8 * 1024 * 1024
RAW_LIMIT = 64 * 1024
TEXT_LIMIT = 64 * 1024


def inspect_binary(path):
    with open(path, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        sample = stream.read(SCAN_LIMIT)
    strings = []
    for encoding, pattern in [('ascii', rb'[\x20-\x7e]{6,}'),
                              ('utf-16le', rb'(?:[\x20-\x7e]\x00){6,}')]:
        for index, match in enumerate(re.finditer(pattern, sample)):
            if index == 1000:
                break
            value = match.group()[:512 if encoding == 'utf-16le' else 256].decode(encoding)
            strings.append(f'0x{match.start():08x} [{encoding}] {value}')
    encoding = 'utf-8-sig'
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = 'utf-16'
    try:
        text = codecs.getincrementaldecoder(encoding)().decode(sample[:TEXT_LIMIT], final=size <= TEXT_LIMIT)
        if any(ord(c) < 32 and c not in '\n\r\t\f' for c in text):
            raise ValueError('Binary control bytes')
        text = f'Text preview ({encoding}), first {min(len(sample), TEXT_LIMIT):,} of {size:,} bytes.\n\n' + text
    except (UnicodeError, ValueError):
        text = 'This data is not plain UTF-8 or BOM-marked UTF-16 text. Use Strings or Raw bytes to inspect it.'
    raw = sample[:RAW_LIMIT]
    hex_text = '\n'.join(f'{i:08x}  {raw[i:i+16].hex(" "):<47}  ' +
                         ''.join(chr(c) if 32 <= c < 127 else '.' for c in raw[i:i+16])
                         for i in range(0, len(raw), 16))
    return {
        'Overview': f'File size: {size:,} bytes\nRead-only binary inspection; the file is not executed.\n'
                    'BIN is a generic extension, not a single format. Raw data cannot automatically be recovered as source code.',
        'Text': text,
        'Strings': f'First {len(sample):,} of {size:,} bytes scanned; up to 1000 strings per encoding, 256 characters each.\n'
                   'Embedded strings are not source code.\n\n' + ('\n'.join(strings) or 'No printable strings found.'),
        'Raw bytes': f'First {len(raw):,} of {size:,} bytes (hexadecimal and ASCII).\n\n' + hex_text,
    }

"""Disposable regex process: timeout enforced by the caller, bounded output."""
import json
import re
import sys

LIMIT = 8 * 1024 * 1024

def replace(text, query, replacement, regex=False, case=False, word=False):
    pattern = query if regex else re.escape(query)
    if word: pattern = r'(?<!\w)(?:' + pattern + r')(?!\w)'
    expression = re.compile(pattern, 0 if case else re.IGNORECASE)
    output, size, count = [], 0, 0
    def append(value):
        nonlocal size
        if '\0' in value: raise ValueError('Replacement cannot insert NUL bytes into a text file')
        size += len(value.encode('utf-8'))
        if size > LIMIT: raise ValueError('Replacement exceeds the 8 MiB file limit')
        output.append(value)
    # Search is line-oriented; replacements must use the same line boundaries.
    for line in text.splitlines(keepends=True):
        content = line.rstrip('\r\n'); ending = line[len(content):]; start = 0
        for match in expression.finditer(content):
            append(content[start:match.start()])
            append(match.expand(replacement) if regex else replacement)
            start = match.end(); count += 1
            if count > 100000: raise ValueError('Too many replacements; narrow the query')
        append(content[start:]); append(ending)
    return ''.join(output), count

if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.buffer.read(LIMIT*6+65536))
        content, count = replace(**payload)
        json.dump({'content': content, 'count': count}, sys.stdout)
    except (ValueError, TypeError, re.error, RecursionError, OverflowError) as error:
        json.dump({'error': str(error)}, sys.stdout)

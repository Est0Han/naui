"""Isolated compatibility regex matcher; cancellation can terminate this process.

Used only when ripgrep is missing. stdin is a bounded single-file snapshot;
stdout is streamed JSON. Never executed in GTK or a non-cancellable worker.
"""
import json
import re
import sys


def main():
    query, sensitive, word = json.loads(sys.argv[1])
    expression = '(?:' + query + ')'
    if word: expression = r'(?<!\w)' + expression + r'(?!\w)'
    pattern = re.compile(expression, 0 if sensitive else re.IGNORECASE)
    for number, line in enumerate(sys.stdin, 1):
        for match in pattern.finditer(line.rstrip('\r\n')):
            # Send only a bounded preview, never a giant matched/minified line.
            start, end = match.span(); offset = max(0, start - 80)
            snippet = ('…' if offset else '') + line[offset:offset + 240].rstrip('\r\n')
            if len(line) > offset + 240: snippet += '…'
            print(json.dumps([number, start, end, snippet]), flush=True)


if __name__ == '__main__': main()

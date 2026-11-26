"""Local PTY bootstrap: private prompt delivery then exec the chosen CLI.

No shell evaluation. stdin mode intentionally gives stdin a finite prompt file;
stdout/stderr still use the real terminal. Interactive modes retain the PTY.
"""
import json
import os
from pathlib import Path
import sys


def main(path):
    path = Path(path)
    config = json.loads(path.read_text())
    path.unlink()  # No durable copy of even the sanitized environment/argv.
    os.chdir(config['cwd'])
    if config['delivery'] == 'stdin':
        fd = os.open(config['prompt_file'], os.O_RDONLY)
        os.dup2(fd, 0)
        if fd != 0: os.close(fd)
    os.execvpe(config['command'][0], config['command'], config['environment'])


if __name__ == '__main__': main(sys.argv[1])

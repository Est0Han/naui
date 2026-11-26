"""Private cache ownership markers and conservative crash leftovers cleanup."""
import json
import os
from pathlib import Path
import shutil
import stat

MARKER = '.naui-cache-owner.json'


def mark_cache(directory, kind):
    marker = Path(directory)/MARKER
    # Runtime-only metadata; no paths, hostnames, credentials or file content.
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump({'format':1, 'kind':kind, 'pid':os.getpid(), 'uid':os.getuid()}, output)


def cleanup_stale_caches(base, kind):
    """Only dead-process, private, marked caches; never unknown older folders.

    No symlinks are followed and no project files outside the cache are removed.
    Limit inspection to 64 entries. Live PID reuse is conservatively left alone.
    """
    if not shutil.rmtree.avoids_symlink_attacks:
        return
    try:
        info = Path(base).lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid(): return
        with os.scandir(base) as entries:
            for index, entry in enumerate(entries):
                if index >= 64: break
                if not entry.name.startswith(kind+'-') or not entry.is_dir(follow_symlinks=False): continue
                directory = Path(entry.path)
                info = directory.lstat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077: continue
                marker = directory/MARKER
                try:
                    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'r') as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 or info.st_uid != os.getuid(): continue
                        record = json.load(source)
                    pid = record.get('pid')
                    if (record.get('format') != 1 or record.get('kind') != kind or record.get('uid') != os.getuid()
                            or type(pid) is not int or pid <= 0): continue
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        shutil.rmtree(directory)
                except (OSError, ValueError, TypeError):
                    continue
    except OSError:
        return

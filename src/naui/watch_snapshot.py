"""Pure bounded remote directory snapshot protocol; no GUI/native imports."""
import posixpath
from decimal import Decimal
from .filesystem import FileEntry, MAX_ENTRIES

REMOTE_WATCH_SNAPSHOT = r'''
parent=${1%/*}; [ -n "$parent" ] || parent=/
if [ ! -e "$1" ] && [ ! -L "$1" ] && [ -r "$parent" ] && [ -x "$parent" ]; then exit 0; fi
[ -d "$1" ] && [ ! -L "$1" ] || { echo 'Watch directory missing or symlink' >&2; exit 1; }
[ -r "$1" ] && [ -x "$1" ] || exit 1
LC_ALL=C find -- "$1" -mindepth 1 -maxdepth 1 -printf '%f\0%y\0%s\0%m\0%T@\0%U:%G\0%D:%i:%m:%s:%T@:%C@:%U:%G\0'
'''


def parse_watch_snapshot(data, path):
    if len(data)>4*1024*1024: raise ValueError('Watch snapshot exceeds 4 MiB')
    fields = data.split(b'\0')
    if fields[-1] != b'' or (len(fields)-1) % 7 or (len(fields)-1)//7 > MAX_ENTRIES:
        raise ValueError('Invalid or oversized watch snapshot')
    result = {}
    for index in range(0,len(fields)-1,7):
        name,kind,size,mode,modified,owner,revision = [value.decode('utf-8') for value in fields[index:index+7]]
        if not name or name in {'.','..'} or '/' in name or len(name.encode())>255: raise ValueError('Invalid watch filename')
        if kind not in {'f','d','l','b','c','p','s','D'} or not revision: raise ValueError('Invalid watch metadata')
        entry=FileEntry(name,posixpath.join(path,name),kind if kind in {'f','d','l'} else 'o',
                        int(size),int(mode,8),int(Decimal(modified)),owner,revision)
        if entry.size<0: raise ValueError('Invalid watch size')
        result[entry.path]=entry
    return result

"""Directory records and transient GNU/Linux remote operations, never `ls` parsing."""
import ctypes
import os
import stat
from dataclasses import dataclass

MAX_ENTRIES = 10000


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    kind: str
    size: int
    mode: int
    mtime: int
    owner: str
    revision: str

    @property
    def is_directory(self):
        return self.kind in {"d", "ld"}

    @property
    def is_symlink(self):
        return self.kind.startswith("l")


def entry_revision(info):
    return f"{info.st_dev}:{info.st_ino}:{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def local_entry(path, name=None):
    name = name if name is not None else os.path.basename(path) or "/"
    name.encode("utf-8")  # GTK/protocol filenames must be valid UTF-8.
    info = os.lstat(path)
    kind = "d" if stat.S_ISDIR(info.st_mode) else "f" if stat.S_ISREG(info.st_mode) else "o"
    if stat.S_ISLNK(info.st_mode):
        kind = "ld" if os.path.isdir(path) else "lf" if os.path.isfile(path) else "l"
    return FileEntry(name, path, kind,
                     info.st_size, stat.S_IMODE(info.st_mode), int(info.st_mtime),
                     f"{info.st_uid}:{info.st_gid}", entry_revision(info))


def rename_no_replace(source, destination):
    """Linux renameat2 makes no-overwrite renaming atomic, including directories."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise OSError("Safe no-overwrite rename is unavailable on this libc")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


# `find -print0` + machine-readable stat fields: names containing whitespace,
# quotes or newlines are data, never lines of human-readable ls output. Remote
# code has no remote Python dependency and all paths are positional arguments.
REMOTE_ENTRY_FUNCTION = r'''
set -e
export LC_ALL=C
record() {
    local p=$1 name=$2 kind=o mode size modified owner rev
    [ -e "$p" ] || [ -L "$p" ] || return 0
    if [ -L "$p" ]; then
        kind=l
        if [ -d "$p" ]; then kind=ld; elif [ -f "$p" ]; then kind=lf; fi
    elif [ -d "$p" ]; then kind=d
    elif [ -f "$p" ]; then kind=f
    fi
    # No -L: keep the selected symlink's identity for rename/delete/properties.
    IFS='|' read -r mode size modified owner rev < <(stat -c '%f|%s|%Y|%u:%g|%d:%i:%f:%s:%y:%z' -- "$p")
    [ -n "$rev" ] || return 1
    printf '%s\0' "$name" "$kind" "$size" "$mode" "$modified" "$owner" "$rev"
}
'''
REMOTE_LIST = REMOTE_ENTRY_FUNCTION + r'''
[ -d "$1" ] || { echo 'Not a directory' >&2; exit 1; }
[ -r "$1" ] && [ -x "$1" ] || { echo 'Directory is not accessible' >&2; exit 1; }
count=0
while IFS= read -r -d '' p; do
    ((count+=1))
    [ "$count" -le 10000 ] || { echo 'Directory exceeds the 10000-entry limit' >&2; exit 1; }
    record "$p" "${p##*/}"
done < <(find -- "$1" -mindepth 1 -maxdepth 1 -print0)
'''
REMOTE_PROPERTIES = REMOTE_ENTRY_FUNCTION + '\nname=${1##*/}; [ -n "$name" ] || name=/; record "$1" "$name"\n'
REMOTE_CREATE = r'''
set -e
umask 077
p=$1
if [ "$2" = directory ]; then
    mkdir -- "$p"
else
    dir=${p%/*}; [ -n "$dir" ] || dir=/
    tmp=$(mktemp -- "$dir/.naui-new-XXXXXXXX")
    trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
    ln -- "$tmp" "$p"
fi
'''
REMOTE_MUTATE = r'''
set -e
export LC_ALL=C
p=$1
current=$(stat -c '%d:%i:%f:%s:%y:%z' -- "$p")
[ "$current" = "$2" ] || { echo 'Selection changed; refresh before retrying' >&2; exit 73; }
if [ "$3" = rename ]; then
    mv -nT -- "$p" "$4"
    [ ! -e "$p" ] && [ ! -L "$p" ] || { echo 'Destination exists; rename refused' >&2; exit 1; }
elif [ -d "$p" ] && [ ! -L "$p" ]; then
    rmdir -- "$p"
else
    rm -- "$p"
fi
'''


def parse_entries(data, parent, root=False):
    fields = data.split(b"\0")
    if fields.pop() != b"" or len(fields) % 7:
        raise ValueError("Invalid directory response")
    if len(fields) // 7 > MAX_ENTRIES:
        raise ValueError("Directory exceeds the 10000-entry limit")
    entries = []
    for index in range(0, len(fields), 7):
        name, kind, size, mode, modified, owner, rev = [x.decode("utf-8") for x in fields[index:index + 7]]
        valid_name = (root and name == "/") or (name not in {"", ".", ".."} and "/" not in name)
        if not valid_name or kind not in {"d", "f", "o", "l", "ld", "lf"}:
            raise ValueError("Invalid directory entry")
        info = FileEntry(name, os.path.join(parent, name), kind, int(size), int(mode, 16) & 0o7777,
                         int(modified), owner, rev)
        if info.size < 0 or not rev or len(name.encode()) > 255:
            raise ValueError("Invalid directory metadata")
        entries.append(info)
    return sorted(entries, key=lambda entry: (not entry.is_directory, entry.name.casefold(), entry.name))

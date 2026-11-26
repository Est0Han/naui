"""Shell-safe explorer navigation and acknowledged, per-filesystem history."""
import posixpath
import shlex

PREPARE_NAVIGATION = b"\x1b[777;1~"
PREPARE_RUN = b"\x1b[777;2~"


def navigation_path(text, cwd, home):
    if not isinstance(text, str) or not text or "\0" in text:
        raise ValueError("Enter a nonempty path without NUL bytes")
    if not cwd or not cwd.startswith("/"):
        raise ValueError("Wait for the shell to report its directory")
    if text == "~" or text.startswith("~/"):
        if not home or not home.startswith("/"):
            raise ValueError("This shell has not reported its home directory")
        text = home + text[1:]
    elif text.startswith("~"):
        raise ValueError("Use ~ or ~/path; ~other-user expansion is not supported")
    path = posixpath.normpath(posixpath.join(cwd, text))
    if len(path.encode("utf-8")) > 4096:
        raise ValueError("Path exceeds the 4096-byte limit")
    return path


def cd_command(path, run_ls=False):
    if not path.startswith("/") or "\0" in path:
        raise ValueError("Navigation requires an absolute path without NUL bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        # Literal controls would be interpreted by readline before the shell
        # sees quoting. Bash/zsh ANSI-C quoting sends only printable bytes.
        quoted = "$'" + "".join(f"\\x{byte:02x}" for byte in path.encode()) + "'"
    else:
        quoted = shlex.quote(path)
    return "cd -- " + quoted + (" && command ls" if run_ls else "") + "\n"


def child_path(parent, name):
    if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name or "\0" in name:
        raise ValueError("Enter a single filename, without / or NUL")
    if len(name.encode("utf-8")) > 255:
        raise ValueError("Filename exceeds 255 bytes")
    return posixpath.join(parent, name)


class NavigationHistory:
    def __init__(self):
        self.paths = []
        self.index = -1
        self.pending_index = None

    @property
    def current(self):
        return self.paths[self.index] if self.index >= 0 else None

    def observe(self, path):
        if self.pending_index is not None:
            index, self.pending_index = self.pending_index, None
            if self.paths[index] == path:
                self.index = index
                return
        if path == self.current:
            return
        del self.paths[self.index + 1:]
        self.paths.append(path)
        self.index = len(self.paths) - 1

    def target(self, step):
        index = self.index + step
        if 0 <= index < len(self.paths):
            self.pending_index = index
            return self.paths[index]
        return None

    def cancel(self):
        self.pending_index = None

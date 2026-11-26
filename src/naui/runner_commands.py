"""Printable Bash/zsh argv quoting for acknowledged terminal input."""
import shlex
from .commands import validate_command
from .navigation import cd_command


def shell_word(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "$'" + ''.join(f'\\x{byte:02x}' for byte in value.encode()) + "'"
    return shlex.quote(value)


def terminal_command(cwd, argv):
    argv = validate_command(argv, cwd)
    quoted = ' '.join(shell_word(value) for value in argv)
    program = shell_word(argv[0])
    error = shell_word(argv[0] + ' not found or not executable on this device')
    return cd_command(cwd).removesuffix('\n') + " && { if command -v -- " + program + " >/dev/null; then " + quoted + "; else printf '%s\\n' " + error + " >&2; (exit 127); fi; }\n"

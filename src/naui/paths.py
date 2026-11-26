"""Path resolution is separate from shell command construction."""
import os
import posixpath
import re


def resolve_path(cwd, path, remote=False):
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("Expected a nonempty filename without NUL bytes")
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd:
        raise ValueError("Expected an absolute working directory")
    # Tilde/glob expansion belongs to the interactive shell, not to this API.
    module = posixpath if remote else os.path
    result = module.normpath(module.join(cwd, path))
    if not remote:
        result = os.path.realpath(result)
    return result


def nano_filenames(args):
    """Small explicit nano CLI subset. Unsupported flags never become filenames."""
    files, options = [], True
    for arg in args:
        if options and arg == "--":
            options = False
        elif options and (arg.startswith("-") or arg.startswith("+")):
            raise ValueError("Naui supports nano FILE [FILE ...] and nano -- FILE; nano flags are not supported. Use /usr/bin/nano for GNU nano.")
        else:
            files.append(arg)
    if not files:
        raise ValueError("Usage: nano FILE [FILE ...]. Use /usr/bin/nano for an unnamed buffer.")
    if len(files) > 32:
        raise ValueError("Open at most 32 files at once")
    return files


def sudo_command(args):
    """Find the command, without mistaking `sudo -u nano echo nano` for an editor.

    This detects the common direct `sudo [OPTIONS] nano` form; the actual sudo
    client still owns option validation, shell modes and authentication.
    """
    value_options = {"-u", "-g", "-h", "-p", "-C", "-R", "-D", "-T", "-r", "-t",
                     "--user", "--group", "--host", "--prompt", "--close-from",
                     "--chroot", "--chdir", "--command-timeout", "--role", "--type"}
    options, index = True, 0
    while index < len(args):
        arg = args[index]
        if options and arg == "--":
            options = False
        elif options and arg in value_options:
            index += 1
        elif options and arg.startswith("-"):
            pass
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arg):
            pass
        else:
            return arg
        index += 1
    return None

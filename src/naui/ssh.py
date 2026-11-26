"""OpenSSH invocation parsing; never implement authentication ourselves."""
from dataclasses import dataclass
import os

# Options affecting the transport are replayed for file operations. Interactive
# flags are accepted separately. Unknown/noninteractive forms pass through ssh.
VALUE_OPTIONS = {"-p", "-l", "-i", "-F", "-J", "-o", "-b", "-c", "-m", "-S", "-E", "-e", "-D", "-L", "-R", "-w", "-O", "-Q", "-W", "-B"}
TRANSPORT_VALUE = {"-p", "-l", "-i", "-F", "-J", "-o", "-b", "-c", "-m", "-S", "-B"}
TRANSPORT_FLAGS = {"-4", "-6", "-C", "-v", "-vv", "-vvv", "-q", "-A", "-a"}
INTERACTIVE_FLAGS = {"-t", "-tt", "-X", "-x", "-Y"}


def ssh_environment():
    # Even with a user's unusually broad `SendEnv *`, the signing key must never
    # travel to a remote server. Keep SSH_AUTH_SOCK and normal OpenSSH settings.
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("NAUI_", "__NAUI_"))}


def validate_transport_options(options):
    index = 0
    while index < len(options):
        arg = options[index]
        if arg in TRANSPORT_FLAGS:
            index += 1
        elif arg in TRANSPORT_VALUE and index + 1 < len(options):
            index += 2
        else:
            raise ValueError("Unsupported SSH transport option")


@dataclass(frozen=True)
class Connection:
    host: str
    options: tuple = ()

    @property
    def username(self):
        if "-l" in self.options:
            return self.options[self.options.index("-l") + 1]
        return self.host.rsplit("@", 1)[0] if "@" in self.host else None

    def command(self, script, *, batch=False):
        options = list(self.options)
        # Read/write runs without a terminal: no password can accidentally enter
        # the file stream. Authentication remains the responsibility of ssh.
        return ["ssh", "-T", "-o", "RequestTTY=no", "-o", "RemoteCommand=none",
                "-o", "SessionType=default", "-o", "ClearAllForwardings=yes",
                "-o", "BatchMode=yes" if batch else "BatchMode=no",
                "-o", "ConnectTimeout=10", *options, "--", self.host, script]


def parse_interactive(args):
    """Return connection options for `ssh [OPTIONS] HOST`, else None.

    Forwarding/control/remote-command invocations retain original OpenSSH behavior.
    Their nano interception is intentionally not advertised as supported.
    """
    options, index, host = [], 0, None
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-"):
            break
        if arg in TRANSPORT_FLAGS:
            options.append(arg)
        elif arg in INTERACTIVE_FLAGS:
            pass
        elif arg[:2] in VALUE_OPTIONS:
            flag = arg[:2]
            if len(arg) > 2:
                value = arg[2:]
            else:
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            if flag not in TRANSPORT_VALUE or flag == "-S":
                return None
            options.extend([flag, value])
        else:
            return None
        index += 1
    if index < len(args):
        host = args[index]
    if not host or host.startswith("-") or index != len(args) - 1 or "\0" in host:
        return None
    return Connection(host, tuple(options))

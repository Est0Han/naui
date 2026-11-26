"""Short-lived LOCAL shell integration entry point. Never copied to a server."""
import os
import signal
import subprocess
import sys
import tempfile
import uuid

from .integration import remote_command
from .paths import nano_filenames
from .protocol import ProtocolError, encode, sign
from .ssh import Connection, parse_interactive, ssh_environment
from .processes import stop_process


class SessionStopped(BaseException):
    def __init__(self, signum):
        self.signum = signum


def _stop_session(signum, frame):
    # Closing a controlling PTY can deliver HUP immediately after TERM.
    # Ignore subsequent shutdown signals before unwinding any finally blocks.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SessionStopped(signum)


def run_child(command, env=None):
    process = subprocess.Popen(command, env=env)
    try:
        return process.wait()
    finally:
        # A second shutdown signal must not interrupt reaping or temporary
        # ControlMaster-directory cleanup. These helpers are LOCAL processes.
        if sys.exc_info()[0] is SessionStopped:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Leave time for socket cleanup before the owning PTY session's
        # one-second fallback deadline kills a nonresponsive helper.
        stop_process(process, grace=0.5)


def emit(action, **fields):
    message = {"v": 1, "token": os.environ["NAUI_TOKEN"], "action": action, **fields}
    frame = encode(sign(message, os.environ["NAUI_LOCAL_KEY"]))
    while frame:
        frame = frame[os.write(sys.stdout.fileno(), frame):]


def open_files(args):
    try:
        files = nano_filenames(args)
        emit("open", location="local", cwd=os.getcwd(), args=files, elevated=False)
        return 0
    except (ValueError, ProtocolError) as exc:
        print(f"Naui: {exc}", file=sys.stderr)
        return 2


def ssh_session(args):
    connection = parse_interactive(args)
    if connection is None:
        # Preserve arbitrary OpenSSH usage verbatim, including scp/control flows.
        return run_child(["ssh", *args], env=ssh_environment())
    session = uuid.uuid4().hex
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    with tempfile.TemporaryDirectory(prefix="naui-ssh-", dir=runtime) as directory:
        socket = os.path.join(directory, "master")
        options = ("-o", "ControlMaster=auto", "-o", "ControlPersist=no",
                   *connection.options, "-S", socket)
        transport = Connection(connection.host, tuple(options))
        emit("session-start", session=session, host=connection.host,
             options=list(transport.options))
        try:
            command = remote_command(os.environ["NAUI_TOKEN"], session)
            # OpenSSH owns all prompts on the inherited real terminal PTY. The
            # multiplex socket is LOCAL and temporary; it permits password-auth
            # sessions to save without a second GUI password implementation.
            return run_child(["ssh", *options, "-tt", "-o", "RemoteCommand=none",
                              "--", connection.host, command], env=ssh_environment())
        finally:
            # A hung-up PTY cannot receive OSC. Preserve the exception so the
            # TemporaryDirectory context still removes the socket directory.
            if sys.exc_info()[0] is not SessionStopped:
                emit("session-end", session=session)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or "NAUI_TOKEN" not in os.environ or "NAUI_LOCAL_KEY" not in os.environ:
        print("Naui shell integration must run inside Naui.", file=sys.stderr)
        return 2
    action, args = args[0], args[1:]
    if action == "open":
        return open_files(args)
    if action in {"cwd", "navigation-ready"}:
        emit(action, location="local", path=os.getcwd(), home=os.path.expanduser("~"))
        return 0
    if action == "busy":
        emit("busy", location="local")
        return 0
    if action == "unsupported":
        reason = " ".join(args)
        emit("unsupported", reason=reason)
        print("Naui: " + reason, file=sys.stderr)
        return 2
    if action == "ssh":
        return ssh_session(args)
    if action == "sudo":
        # Native sudo/editor handle the inherited PTY, permissions and passwords.
        return run_child(["sudo", *args])
    return 2


if __name__ == "__main__":
    signal.signal(signal.SIGHUP, _stop_session)
    signal.signal(signal.SIGTERM, _stop_session)
    try:
        raise SystemExit(main())
    except SessionStopped as exc:
        raise SystemExit(128 + exc.signum)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except OSError as exc:
        print(f"Naui: {exc}", file=sys.stderr)
        raise SystemExit(1)

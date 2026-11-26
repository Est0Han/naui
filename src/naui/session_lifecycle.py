"""Pure terminal-child exit policy, shared by frontends and tests."""
from dataclasses import dataclass
from enum import Enum


class TerminalLifecycle(Enum):
    RUNNING = 'running'
    CLOSING = 'closing'
    CLOSED = 'closed'


@dataclass(frozen=True)
class ExitDecision:
    action: str
    message: str


def classify_exit(*, remote, returncode, other_terminals):
    if remote:
        return ExitDecision('close_tab', 'SSH terminal ended; Naui and other devices remain open.')
    if other_terminals:
        return ExitDecision('close_tab', 'Terminal ended; other terminal sessions remain open.')
    if returncode == 0:
        # Intent cannot be proven from status alone; this also covers Ctrl+D and
        # a shell that returned normally without an explicit `exit` command.
        return ExitDecision('confirm_exit', 'The local shell has exited.')
    description = f'Signal {-returncode}' if returncode is not None and returncode < 0 else f'Exit code: {returncode}'
    return ExitDecision('restart_or_close', 'Terminal process exited unexpectedly. ' + description)

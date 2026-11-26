"""Expose virtual remote paths from canonical paths in isolated test machines."""
from dataclasses import replace
from pathlib import Path
from test_workspace import RootedTransport


def connect_virtual_machine(backend, host, root):
    backend.connect(RootedTransport(host, root))
    snapshot = backend._delegate._snapshot
    def decode(response):
        result = snapshot(response)
        # The real Bash/coreutils script reports its canonical physical path.
        # Translate that one response field to the simulated machine's POSIX
        # namespace; commands, bytes and revision checking remain unchanged.
        path = '/' + Path(result.path).relative_to(Path(root)).as_posix()
        return replace(result, path=path)
    backend._delegate._snapshot = decode

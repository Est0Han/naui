"""Local command workspace and SSH command helper; no remote helper installation."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys


def remote_command(connection, cwd, argv):
    if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd:
        raise ValueError('An absolute remote working directory is required')
    if not argv or any(not isinstance(a, str) or '\0' in a for a in argv):
        raise ValueError('A remote command argument list is required')
    return connection.command('cd -- ' + shlex.quote(cwd) + ' && exec ' + shlex.join(argv), batch=True)


def prepare_workspace(plugin_id, device, cwd, existing=None):
    from .ssh import validate_transport_options
    connection = device.filesystem_backend.connection
    validate_transport_options(connection.options)
    # Validate before creating any files. No connection or model call here.
    remote_command(connection, cwd, ['pwd'])
    identity = hashlib.sha256(json.dumps([device.id, connection.host, connection.options, cwd]).encode()).hexdigest()
    parent = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'naui'/'agent-ssh'/plugin_id
    root = parent/identity
    if existing is not None:
        candidate = Path(existing)
        if candidate.is_symlink() or candidate.resolve().parent != parent.resolve():
            raise ValueError('Saved SSH workspace is outside this plugin cache')
        config = candidate/'connection.json'
        if config.is_symlink() or not config.is_file() or config.stat().st_size > 65536:
            raise ValueError('Saved SSH workspace metadata is unavailable')
        previous = json.loads(config.read_text())
        if previous.get('host') != connection.host or previous.get('cwd') != cwd:
            raise ValueError('Saved SSH workspace host or project changed')
        root = candidate
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    config = root/'connection.json'
    payload = json.dumps({'host': connection.host, 'options': connection.options, 'cwd': cwd})
    # Write privately and atomically, including when an earlier task exists.
    import tempfile
    fd, temporary = tempfile.mkstemp(dir=root)
    try:
        with os.fdopen(fd, 'w') as stream: stream.write(payload)
        os.replace(temporary, config)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {'cwd': str(root), 'command': [sys.executable, str(Path(__file__).resolve()), str(config)],
            'device_id': device.id, 'remote_cwd': cwd}


def main():
    # Executed by a plugin-managed local command.
    # This is a remote shell helper, not a filesystem sandbox.
    from naui.ssh import Connection, ssh_environment
    if len(sys.argv) < 3:
        raise SystemExit('Usage: ssh_workspace.py CONNECTION_FILE PROGRAM [ARGUMENT ...]')
    config = json.loads(Path(sys.argv[1]).read_text())
    command = remote_command(Connection(config['host'], tuple(config['options'])), config['cwd'], sys.argv[2:])
    import subprocess
    raise SystemExit(subprocess.call(command, env=ssh_environment()))


if __name__ == '__main__':
    # Resolve the installed package independently of the caller's environment.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()

"""Python provider: local Pyright invocation and language-specific settings."""
import os
import shlex
import shutil

def pyright_command():
    from .base_lsp.transport import LspError
    configured = os.environ.get("NAUI_PYRIGHT_COMMAND")
    if configured:
        command = shlex.split(configured)
        if not command:
            raise LspError("NAUI_PYRIGHT_COMMAND is empty")
        return command  # User-owned argv; never evaluated by a shell.
    executable = shutil.which("pyright-langserver")
    if not executable:
        raise LspError("Pyright is unavailable. Install it locally: npm install --global --prefix ~/.local pyright")
    return [executable, "--stdio"]


def start(api):
    from .base_lsp.workspace import LspSession
    class PyrightSession(LspSession):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('command_factory', pyright_command)
            kwargs.setdefault('settings', {'python': {'analysis': {'diagnosticMode': 'openFilesOnly', 'typeCheckingMode': 'basic'}}})
            super().__init__(*args, **kwargs)
    api.register_feature('languages', {'.py': PyrightSession, '.pyi': PyrightSession})

"""Scaffolding and AI instructions with real checkout/installation SDK paths."""
import json
import sys
from pathlib import Path
from .plugin_manifest import API_VERSION, PERMISSIONS, valid_id


def resource_root():
    checkout = Path(__file__).resolve().parents[2]
    return checkout if (checkout/'docs/plugin-development').is_dir() else Path(sys.prefix)/'share/naui'


def sdk_root(): return resource_root()/'docs/plugin-development'


def examples_root(): return resource_root()/'examples/plugins'


def agent_prompt(directory, request, permissions, *, mode='create', errors=''):
    root = Path(directory).expanduser().resolve()
    sdk, examples = sdk_root(), examples_root()
    return f'''You are {'implementing' if mode == 'create' else 'modifying'} a Naui plugin.

PLUGIN DIRECTORY:
{root}

You may modify ONLY files inside this directory: {root}/**
You may modify ONLY:
{root}/**
DO NOT modify Naui core, the Plugin API, installer, terminal/session, SSH,
authentication, TransferManager, FileBackend, application lifecycle, bundled/base
plugins, unrelated plugins or system/SSH/shell configuration.

First inspect the installed CLI (do not guess paths):
naui --version
naui plugin api-version
naui plugin sdk-path
naui plugin directory

Before implementing, read:
{sdk}/AGENT_INSTRUCTIONS.md
{sdk}/API_REFERENCE.md
{sdk}/UI_GUIDE.md
{sdk}/SECURITY.md
{sdk}/MANIFEST.md
Relevant reference examples (read only):
{examples}/hello-world
{examples}/sidebar-panel
{examples}/right-panel
{examples}/file-action
{examples}/terminal-command

Requested {'plugin' if mode == 'create' else 'change'}:
{request}

Requested permissions:
{', '.join(sorted(permissions)) or 'none'}

{('Validation error:\n' + errors + '\n') if errors else ''}
Rules:
- Use only documented public Naui Plugin API v{API_VERSION} from naui_plugin_api.
- Do not import private Naui internals or access underscore-prefixed API members.
- Do not implement SSH credential handling, read SSH private keys, or store passwords.
- Do not bypass FileBackend or TransferManager for device files.
- Do not use raw subprocesses when a documented Naui API already exists.
- For persistent right-side UI use api.ui.panel(), focus_panel(), and remove_panel(); check api.ui.supports('right_panels'). See the right-panel example.
- Keep GTK widgets and credentials core-owned. Do not quit the application.
- Preserve existing functionality and tests; make deactivate() idempotent.
- If a required capability is missing, STOP and report exactly:
  PLUGIN_API_CAPABILITY_MISSING: <description>

Implement the plugin and unittest tests. Run this validator before finishing:
naui plugin validate {__import__('shlex').quote(str(root))}
The validator imports/tests trusted code; it is not a security sandbox.
CLI agents may maintain their own session history. Never put credentials in this task.
'''


def scaffold(parent, identity, name, description, permissions):
    if not valid_id(identity): raise ValueError('Invalid/reserved plugin ID')
    if not isinstance(name, str) or not name.strip() or len(name) > 128 or '\0' in name: raise ValueError('Plugin name required (max 128 characters, no NUL)')
    if not isinstance(description, str) or len(description) > 8192 or '\0' in description: raise ValueError('Description exceeds 8192 characters or contains NUL')
    permissions = frozenset(permissions)
    if permissions - PERMISSIONS: raise ValueError('Unknown plugin permission')
    parent = Path(parent).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    root = parent/identity
    root.mkdir(mode=0o700)  # Exclusive: never overwrite an existing plugin.
    try:
        (root/'.gitignore').write_text('# Generated personal plugin; remove this rule to version it.\n*\n', encoding='utf-8')
        manifest = (f'id = {json.dumps(identity)}\nname = {json.dumps(name, ensure_ascii=False)}\nversion = "0.1.0"\n'
                    f'api_version = {API_VERSION}\nentrypoint = "plugin.py"\n'
                    f'description = {json.dumps(description, ensure_ascii=False)}\nsource = "ai-created"\n\n[permissions]\n')
        manifest += ''.join(f'{permission} = true\n' for permission in sorted(permissions))
        (root/'plugin.toml').write_text(manifest, encoding='utf-8')
        (root/'plugin.py').write_text('''"""AI scaffold: implement only the requested behavior through the public API."""
from naui_plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def activate(self, api):
        self.api = api
        # TODO: implement the behavior described in README.md.
        # api.notifications.show("Plugin activated") works without permissions.

    def deactivate(self):
        # Core unregisters API-owned commands/views/timers/watchers.
        # TODO: stop any other activity this plugin owns. Must be idempotent.
        self.api = None
''', encoding='utf-8')
        (root/'README.md').write_text(f'# {name}\n\n{description}\n\nScaffold only; requested behavior is not implemented yet.\n'
                                    'Use the generated AI prompt and Naui Plugin SDK.\n', encoding='utf-8')
        (root/'tests').mkdir()
        (root/'tests/test_plugin.py').write_text('''import unittest
from plugin import Plugin


class ScaffoldTests(unittest.TestCase):
    def test_lifecycle_is_idempotent(self):
        plugin = Plugin()
        plugin.activate(object())
        plugin.deactivate()
        plugin.deactivate()
        self.assertIsNone(plugin.api)
''', encoding='utf-8')
    except BaseException:
        import shutil
        shutil.rmtree(root)
        raise
    return root

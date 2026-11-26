"""Display-independent plugin validation and SDK discovery commands."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from .plugin_manifest import API_VERSION, plugin_directory, inspect_manifest, valid_id, fingerprint
from .plugin_sdk import sdk_root, examples_root, resource_root
from .plugin_validation import PluginValidator, static_validate
from .plugin_storage import PluginStore


def main(argv):
    parser = argparse.ArgumentParser(prog='naui plugin')
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate', help='Validate/import/run tests for TRUSTED plugin code (not a sandbox)')
    validate.add_argument('directory')
    validate.add_argument('--static', action='store_true', help='Do not execute imports/tests')
    validate.add_argument('--json', action='store_true', help='Machine-readable validation result')
    commands.add_parser('api-version')
    commands.add_parser('sdk-path')
    commands.add_parser('directory')
    commands.add_parser('paths')
    commands.add_parser('list')
    for name in ('enable', 'disable', 'uninstall'):
        operation = commands.add_parser(name)
        operation.add_argument('id')
        if name == 'enable': operation.add_argument('--trust', action='store_true', help='Trust declared permissions and imports/tests; enable on next normal launch')
        if name == 'uninstall': operation.add_argument('--yes', action='store_true', help='Confirm removal after closing Naui; settings retained')
    args = parser.parse_args(argv)
    if args.command == 'api-version': print(API_VERSION); return 0
    if args.command == 'sdk-path': print(sdk_root()); print(examples_root()); return 0
    if args.command == 'directory': print(plugin_directory()); return 0
    if args.command == 'paths':
        print(json.dumps({'installation': str(resource_root()), 'sdk': str(sdk_root()),
                          'examples': str(examples_root()), 'plugins': str(plugin_directory()), 'api_version': API_VERSION}, indent=2))
        return 0
    store = PluginStore()
    if args.command == 'list':
        if store.error: print('State error: ' + store.error, file=sys.stderr)
        if store.directory.is_dir():
            for root in sorted(store.directory.iterdir()):
                if not root.is_dir() or root.name.startswith('.'): continue
                result = static_validate(root)
                saved = store.data['plugins'].get(root.name, {})
                state = ('failed/recovery' if saved.get('startup_pending') or saved.get('recovery_required') else
                         'enabled for next launch' if saved.get('enabled') else 'disabled')
                print(root.name + ' · ' + state + (' · invalid: ' + '; '.join(result.errors) if not result.ok else ''))
        return 1 if store.error else 0
    if args.command in {'enable', 'disable', 'uninstall'}:
        try:
            if not valid_id(args.id): raise ValueError('Invalid/reserved user plugin ID')
            root = store.directory/args.id
            if root.is_symlink() or not root.is_dir(): raise ValueError('No managed plugin folder with this ID')
            old = store.data['plugins'].get(args.id, {})
            if args.command == 'enable':
                spec, _, _ = inspect_manifest(root)
                if spec.id != args.id: raise ValueError('Manifest identity does not match folder')
                print('Requested permissions: ' + (', '.join(sorted(spec.permissions)) or 'none'))
                if not args.trust: raise ValueError('Review code/permissions, then repeat with --trust. This executes trusted Python.')
                validator = PluginValidator()
                try: result = validator.validate(root, execute=True)
                finally: validator.close()
                print(result.text())
                if not result.ok: return 1
                if fingerprint(root) != result.fingerprint: raise ValueError('Plugin changed after validation')
                store.remember(args.id, True, spec.permissions, result.fingerprint, spec.source)
                print('Enabled for next normal launch. CLI does not activate GUI plugins in an unrelated running process.')
            elif args.command == 'disable':
                store.remember(args.id, False, old.get('grants', []), old.get('fingerprint', ''), old.get('source', 'local'))
                print('Disabled for next launch. Use the running GUI to deactivate immediately.')
            else:
                if not args.yes: raise ValueError('Uninstall ' + str(root) + '? Repeat with --yes after closing Naui; settings retained')
                store.remember(args.id, False, old.get('grants', []), old.get('fingerprint', ''), old.get('source', 'local'))
                shutil.rmtree(root)
                store.data['plugins'].pop(args.id, None); store.save()
                print('Uninstalled ' + args.id + '; settings retained')
            return 0
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr); return 1
    directory = args.directory
    if valid_id(directory) and not Path(directory).is_dir(): directory = str(store.directory/directory)
    validator = PluginValidator()
    try: result = validator.validate(directory, execute=not args.static)
    finally: validator.close()
    if args.json:
        print(json.dumps(dict(ok=result.ok, checks=result.checks, errors=result.errors,
                              warnings=result.warnings, executed=result.executed, fingerprint=result.fingerprint)))
    else: print(result.text())
    return 0 if result.ok else 1

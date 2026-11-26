# Plugin development checks

For one trusted plugin (imports/tests execute Python):

```bash
naui plugin validate /absolute/path/to/plugin
naui plugin validate /absolute/path/to/plugin --json
```

For code-free inspection use `--static`. Passing static inspection does not
authorize enabling; Naui still requires permission/trust review and import/test
validation. The validator checks lifecycle signatures and constructs the plugin;
it does **not** call activate against a live GUI. Test activate with a small mock
API in your own unittest tests, then enable in Naui to check real integration.

The four shipped examples include tiny mock-API tests. Use relative imports for
plugin helpers. Naui recompiles source on reload, including helpers, rather than
reusing stale bytecode. If import/tests fail, the running older plugin stays
active; if live activation fails, the replacement is marked failed/disabled and
API-owned resources are removed. Naui cannot roll back arbitrary Python effects.

From the Naui checkout:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p test_plugin_sdk.py -v
PYTHONPATH=src python3 -m unittest discover -s tests -p test_installation.py -v
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/plugin_manager_smoke.py
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/terminal_tabs_smoke.py
```

The GUI plugin smoke runner creates disposable configuration/cache. It uses the
real scaffold dialog, prompt generation/copy, permission review, validation and
ten enable/reload cycles with native panels/browser/text tabs and forced GC.
It also tests a forbidden import, ZIP install/rejection/uninstall, surviving
terminal input, and application close with a manager-owned dialog still open.
It never changes the desktop runtime directory or accesses an SSH server.
Both GUI runners return 77/SKIP if no display is accessible; this is not a pass.
The Linux CI workflow runs them on Xvfb/D-Bus with fatal warnings.

Manual review also covers the modification prompt, folder/terminal buttons,
truncated metadata, logs, permission changes and disabled-state restoration
after restart. Test plugins are trusted Python, not malware fixtures. Use only
disposable local test directories for failure cases. No GUI test here proves
real SSH/device behavior; those retain their separate authorized acceptance.

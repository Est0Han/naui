# Naui plugin development instructions for AI agents

If the requested feature can be implemented through the public Plugin API,
**DO NOT modify Naui core.**

You may modify only the assigned plugin directory. You may read this SDK and
`examples/plugins/`, but do not modify Naui core, terminal/session internals,
SSH internals, authentication internals, TransferManager internals, the Plugin
API implementation, installer, bundled adapters, or unrelated plugins unless
the user explicitly requests a core/API change.
Do not modify FileBackend internals, application lifecycle or system/SSH/shell
configuration. Before writing code run `naui --version`, `naui plugin api-version`,
`naui plugin sdk-path` and `naui plugin directory` to locate the actual installed
SDK and assigned user-plugin root; do not guess installation paths.

1. Read API_REFERENCE.md, UI_GUIDE.md, MANIFEST.md and SECURITY.md.
2. Use `from naui_plugin_api import Plugin, FileLocation`. Define `class Plugin`
   with `activate(api)` and idempotent `deactivate()`. Legacy `start(api)` and
   returned `stop()` objects remain compatible.
3. Declare API version 1 and only needed permissions in plugin.toml. Implement
   behavior in this plugin directory, with UTF-8 source and unittest tests.
4. Preserve originating device IDs in file, editor, terminal and transfer
   operations. Use public API objects, not private modules or `_host`/`_api`.
5. Keep callbacks short. File/command/network callbacks return on the GUI
   thread. Native widgets, PTYs, SSH transport and authentication stay core-owned.
6. Core unregisters API-owned commands/views/watchers/timers on unload. Stop
   any other activity you own during deactivate; do not destroy native widgets,
   quit Naui, or kill terminal sessions. Do not leave background threads running.
7. Run `naui plugin validate /absolute/assigned/plugin/directory`. This executes
   trusted plugin imports/tests in a separate interpreter; it is not a sandbox.
8. In Naui: Plugins → Validate → Enable, then Modify → Validate → Reload.

If the public API lacks a required capability, STOP and report exactly:

`PLUGIN_API_CAPABILITY_MISSING: <description>`

Do not bypass the boundary to work around missing capabilities.

A plugin must not implement SSH credential storage, read private SSH keys, or
store passwords/tokens. It must not modify core terminal/session internals or
bypass FileBackend / TransferManager for device files. Use argv through public
command/terminal APIs instead of raw subprocesses. Opening a file never grants
permission to execute it. Execute only on an explicit user action.

Persistent right-side panels are supported by the additive v1 UI extension:
check `getattr(api.ui, 'supports', lambda _: False)('right_panels')`, then use
`api.ui.panel(...)`, `focus_panel(id)`, and `remove_panel(id)`. Read the API
reference and `examples/plugins/right-panel`. Use `views`, plus `commands` for
a composer. Do not report right-side text/input/action panels as unsupported on
an installation that advertises this capability. Native terminal embedding is
not part of this extension; terminal runs still open core terminal tabs.

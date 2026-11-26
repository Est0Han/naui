# Public Naui Plugin SDK v1

Import `Plugin`, `FileLocation`, `DeviceInfo`, `DocumentInfo`, `PluginAPI` and
`API_VERSION` from **naui_plugin_api**. Other `naui.*` imports are private for
user plugins. No GTK, backend, SSH, terminal widget or credential object is public.

```python
from naui_plugin_api import Plugin as BasePlugin

class Plugin(BasePlugin):
    def activate(self, api):
        api.commands.add("hello", "Hello", lambda: api.notifications.show("Hello"))
    def deactivate(self):
        pass  # Core removes every resource registered through the API.
```

All API calls originate on the GUI thread. I/O runs in core workers, and
completion callbacks return on that thread as `(value, error)` (except create,
whose callback is `(error)`). Callbacks are ignored after deactivation and
exceptions fail only the originating plugin. Plugin-defined background activity
must be stopped explicitly in deactivate. Python is trusted, not sandboxed.

`FileLocation(device_id, absolute_path)` is immutable. Preserve the originating
device identity; never resolve remote files against the selected local tree.
Every operation on SSH devices additionally requires **ssh** (legacy
remote_access). No operation exposes credentials. Missing permissions raise
PermissionError. Device disconnects/conflicting writes report errors.

## Commands, notifications and lifecycle

| Call (purpose / arguments) | Permission | Result / example |
| --- | --- | --- |
| commands.add(id, label, callback) — register explicit action | commands | None; `api.commands.add('hello', 'Hello', lambda: api.notifications.show('Hi'))` |
| notifications.show(text) — short status | none | None; `api.notifications.show('Done')` |
| logs.write(text) — bounded session plugin log | none | None; `api.logs.write('Refresh complete')`; never log secrets |
| defer(milliseconds, callback) — one-shot timer | none | source ID; `timer = api.defer(500, refresh)` (0–60000 ms) |
| cancel_deferred(id) — cancel owned timer | none | None; `api.cancel_deferred(timer)` |
| workspace.on_changed(callback) — device/root/context notification | filesystem_read | None; `api.workspace.on_changed(refresh)` |

## Workspace and editor

| Call | Permission | Result / example |
| --- | --- | --- |
| devices.list() — available device metadata | filesystem_read | tuple of DeviceInfo; `devices = api.devices.list()` |
| devices.selected() — active device | filesystem_read | DeviceInfo or None; `device = api.devices.selected()` |
| workspace.selected_directory() — selected tree folder or file parent, preserving device | filesystem_read (+ remote_access for SSH) | FileLocation or None |
| workspace.selected_location() — selected tree path | filesystem_read | FileLocation or None; `location = api.workspace.selected_location()` |
| editor.current() — active editor snapshot | editor_read | DocumentInfo(location, text, dirty) or None; `doc = api.editor.current()` |
| editor.open(location, line=0, column=0) — open viewer / navigate | filesystem_read, editor_navigation | None; `api.editor.open(doc.location, 10, 2)`; positions zero-based UTF-16 |
| editor.add_action(id, label, callback) — command receiving snapshot | commands, editor_read | None; `api.editor.add_action('inspect', 'Inspect Editor', lambda doc: api.notifications.show(doc.location.path if doc else 'No editor'))` |
| editor.invoke(command) — undoable command on active buffer; does not save | commands, editor_read, filesystem_write | bool: whether changed; `api.editor.invoke('toggle_comment')` |

DeviceInfo contains id/type/display_name/state/current_path/roots, home and active,
never a live Device or backend. Editor text is a copied snapshot. `editor.invoke`
accepts `toggle_comment`, `indent`, `outdent`, `move_up`, `move_down`, `duplicate`,
and `delete_line`. It uses that document's current selection and device; remote
documents additionally require `remote_access`. It never exposes widgets,
executes files or saves automatically. Register a plugin command with `add_action`
and invoke a line command from its callback. Arbitrary native buffer access or
language-server registration is not supported in this API.

## Filesystem and watches

| Call | Permission | Result / example |
| --- | --- | --- |
| files.read(location, callback) — UTF-8 text | filesystem_read | callback(text, error); `api.files.read(location, lambda text, error: api.notifications.show(str(error) if error else text[:80]))` |
| files.snapshot(location, callback) — text and revision | filesystem_read | callback(FileSnapshot(path, content, revision, mode), error); `api.files.snapshot(location, loaded)` |
| files.write(location, content, expected, callback) — atomic revision-checked save | filesystem_write | callback(new FileSnapshot, error); `api.files.write(location, snapshot.content, snapshot.revision, saved)`; never unconditional overwrite |
| files.properties(location, callback) — metadata | filesystem_read | callback(FileEntry, error); `api.files.properties(location, inspected)` |
| files.exists(location, callback) — existence | filesystem_read | callback(bool, error); `api.files.exists(location, found)` |
| files.create(location, directory=False, callback=None) — exclusive creation | filesystem_write | callback(error); `api.files.create(location, directory=True, callback=created)` |
| files.add_action(id, label, predicate, callback) — clicked-item menu action | commands, filesystem_read | None; `api.files.add_action('inspect', 'Inspect', lambda entry: entry.kind == 'f', lambda location, entry: api.notifications.show(location.path))` |
| files.watch(device_id=..., path=..., callback=..., recursive=False) | filesystem_read, filesystem_watch | handle.close(); `watch = api.files.watch(device_id='local', path='/tmp', callback=changed)` |
| files.observe(callback) — shared watcher events | filesystem_read, filesystem_watch | handle.close(); `observer = api.files.observe(changed)` |

Read/write limits are 8 MiB UTF-8, no binary/NUL data. A snapshot revision of
`missing` allows creation; any other revision checks the original contents.
Watch events have device_id, event, path and optional old_path. Core uses native
local watching and bounded remote watching/polling; do not create your own
filesystem poller. Nonrecursive subscriptions are preferred.

## Execution and transfers

| Call | Permission | Result / example |
| --- | --- | --- |
| terminals.run(device_id, cwd, command, new_terminal=False) (keyword args) | terminal | owned run ID; `run = api.terminals.run(device_id=location.device_id, cwd='/tmp', command=['pwd'], new_terminal=True)` |
| terminals.interrupt(id) — Ctrl+C for owned run | terminal | None; `api.terminals.interrupt(run)` |
| commands.execute(device_id, cwd, command, callback) (keyword args) — bounded background output | command_execution | callback(bytes, error); `api.commands.execute(device_id='local', cwd='/tmp', command=['git','status','--porcelain=v2','-z'], callback=done)` |
| transfers.copy(source, destination) — central copy job | file_transfer, filesystem_read, filesystem_write | owned job ID; `job = api.transfers.copy(source, destination)` |
| transfers.status(id) — progress/state | file_transfer | plain status dict; `status = api.transfers.status(job)` |
| transfers.cancel(id) — cancel owned transfer | file_transfer | None; `api.transfers.cancel(job)` |

Use argv, never shell strings. Core handles PTY readiness, quoting, device
selection, OpenSSH and authentication. Destination is the **full target path**,
not a directory. Transfer API supports local→local, local→SSH, SSH→local and
SSH→SSH through bounded local streaming. Core handles conflicts/auth prompts;
plugins can control only their own job IDs. Move/Pause API is not exposed yet.

## Views, input, settings and network

| Call | Permission | Result / example |
| --- | --- | --- |
| ui.sidebar(id, title, rows) — core-owned left panel | views | None; `api.ui.sidebar('info', 'INFO', [{'text':'Ready'}])` |
| ui.open_text(device_id, title, content, language=None) — main read-only tab | views | None; `api.ui.open_text('local', 'Output', 'Hello')` |
| ui.open_browser(device_id, title, refresh, more) — list/detail view | views | BrowserHandle; see UI_GUIDE.md |
| ui.input(title, fields, callback) — temporary text fields | commands | callback(list of strings) on acceptance; `api.ui.input('Arguments', [('Args','')], entered)` |
| ui.confirm(title, message, button, callback) — explicit confirmation | commands | callback() only on acceptance; `api.ui.confirm('Run?', 'Execute saved file?', 'Run', launch)` |
| settings.get(key, default=None) — own plugin JSON settings | none | value; `size = api.settings.get('size', 10)` |
| settings.set(key, value) — persist own safe settings | none | None; `api.settings.set('size', 20)`; password/token/credential keys rejected |
| network.request(url, callback, method='GET', data=None) — bounded HTTP(S) | network | callback(bytes, error); `api.network.request('https://example.org', done)` |

Settings/enable grants use user config, never project files. Do not save secrets,
authentication or sensitive command arguments. Network HTTP GET/POST timeout:
10 seconds, request max 1 MiB, response max 8 MiB. Arbitrary long-running network
services/MQTT clients need a documented background-task API (not provided yet).
Report `PLUGIN_API_CAPABILITY_MISSING` instead of blocking the GUI or accessing
private internals.

## Compatibility and bundled adapters

Legacy flat methods remain: add_command/message/devices()/selected_location/
current_document/read_text/properties/exists/create/open/on_workspace_changed/
add_file_action/add_shortcut/confirm/input/sidebar/open_text/open_browser.
Legacy terminal_execution/remote_access names remain. The same facade is used by
base-runner and base-git; their command/watch/UI/device operations remain public.
base-file-explorer, base-lsp, base-filepath, python-lsp and base-media still use a
restricted bundled factory adapter to preserve existing GTK implementations.
User plugins cannot register those factories or disable/uninstall essential
bundled adapters while Naui is running. Process isolation of live plugins is
future work; validation subprocesses only isolate import/test failures.


## Configurable commands

`api.commands.register(id, title, callback, *, default_shortcut=None,
context="global", enabled=None)` requires `commands`. Returns the registered,
plugin-namespaced command ID. Supported contexts: global, editor, terminal,
file_tree, sidebar, git, plugin. `enabled` is an optional zero-argument boolean callable.
Default shortcuts are suggestions; users customize them in Settings → Keybindings.
Duplicate defaults in one context leave all conflicting commands unbound.
Registration is owned and removed automatically on deactivate/reload, while
user overrides survive. Terminal Ctrl+C/D/Z are protected PTY input.

```python
api.commands.register("hello", "Hello", lambda: api.notifications.show("Hello"),
                      default_shortcut="Ctrl+Alt+H", context="global")
```

`api.commands.add(id, title, callback)` remains supported without a default.
No private widgets, SSH secrets or keybinding files are exposed to plugins.

## Reusable diff and compact sidebar extensions (API v1)

`api.ui.open_diff(device_id, title, patch, *, location=None, repository=None,
staged=False, historical=False)` opens a read-only main-area unified diff, with Side by Side /
Inline modes and hunk navigation. Requires `views`; supplying `location` also
requires `filesystem_read` and `editor_navigation`. `location` is a public
`FileLocation` on the same device and enables Open File. `repository` is the
logical root, never an SSHFS mount path. Returns `None`. Patch output is bounded
at 8 MiB; large renderings require explicit approval in the view.

```python
api.ui.open_diff(device_id, "Diff: config.py", patch,
                 location=location, repository=logical_root)
```

`api.ui.current_diff()` (`views`) returns a plain context dictionary for an
active file diff (`device_id`, `repository`, `location`, `staged`), or `None`.
It never exposes a widget. `api.ui.copy_text(text)` (`views`) copies at most
64 KiB of text, returning `None`. `api.files.reveal(location)`
(`filesystem_read`, `editor_navigation`) reveals the logical file directory in
its device tree without changing a terminal's cwd. `api.ui.focus_sidebar(id)`
(`views`) expands/focuses the calling plugin's sidebar section.

Sidebar row dictionaries retain `text` and optional `actions` (pairs of title
and callback). Additional optional fields: `tooltip`, `select` (default click),
`menu` (compact overflow/right-click actions), `children` (collapsible rows),
`id` (stable expansion identity), and `expanded` (initial state). Up to 2,000
rows / four nesting levels; all callbacks are guarded and cleanup belongs to
Naui. An optional `input` dictionary contains `value`, `placeholder` and
`changed(text)` for a draft field. The callback must update plain state without
rebuilding the widget in that signal.

```python
api.ui.sidebar("status", "STATUS", [{"text": "Files", "id": "files",
    "children": [{"text": "config.py", "select": open_diff,
                  "menu": [("Open File", open_file)]}]}])
```

`api.files.probe(location, callback)` (`filesystem_read`) asynchronously returns
up to 8 KiB of file bytes via `callback(sample, error)`, without backend objects.
`api.files.remove(location, expected_revision, callback)` (`filesystem_write`)
deletes one file/symlink, refuses directories, and checks the revision from
`api.files.properties`. It respects active-transfer mutation protections.
Returns `None`; completion is `callback(result, error)`. Ask for confirmation
before destructive user operations. No recursive directory deletion is offered.

### Activity panels

`api.ui.sidebar(id, title, rows)` requires `views` and returns None. The host
registers a persistent activity panel; repeated calls replace rows in that panel.
`api.ui.focus_sidebar(id)` requires `views` and returns None; it selects an
existing plugin-owned panel. `api.ui.set_sidebar_badge(id, count)` requires
`views` and returns None; register the panel first, then provide a nonnegative
integer count (up to 1,000,000).

```python
api.ui.sidebar("monitor", "Monitor", [{"text": "Ready"}])
api.ui.set_sidebar_badge("monitor", 2)
api.ui.focus_sidebar("monitor")
```

Disabling/unloading removes that plugin's activity panels and callbacks. Active
selection falls back to Explorer. Main tool views remain in the main content area.

## Right-side panels (additive API v1 extension)

Use `getattr(api.ui, 'supports', lambda _: False)('right_panels')` to detect this
extension on older installations. API version stays 1; existing plugins retain
their behavior. `supports` currently reports `right_panels`, `panel_text`, and
`panel_input`. Unknown names return False. Discovery needs no extra permission.

| Call | Permission | Behavior |
| --- | --- | --- |
| `ui.panel(id, title, rows=(), *, text='', on_submit=None, placeholder='Message…')` | views; commands also required with on_submit | Create/update an owned right-side panel; returns None |
| `ui.focus_panel(id)` | views | Select and reveal an existing owned panel |
| `ui.remove_panel(id)` | views | Defer removal of an owned panel; missing IDs are harmless |

IDs are 1–64 ASCII letters/digits/hyphens/underscores and are scoped to the plugin.
Titles/placeholders are at most 200 UTF-8 bytes. `text` is selectable plain text,
limited to 256 KiB (no markup/HTML execution). `rows` uses the existing sidebar
row schema, with 200 rows, nesting depth 4 and 256 KiB total row text. Each row
can provide named actions, menus or input; callbacks use the normal plugin guard.
Row-input `changed` handlers must update plain draft state only, not rebuild UI
inside the originating GTK callback.

`on_submit(message)` enables a core-owned multiline composer with a Send button.
It receives at most 16000 UTF-8 bytes after an explicit user action, on a deferred GUI turn. Oversized drafts are preserved.
Submission clears the input; store anything your workflow needs in plugin state.
Updating the panel preserves unsent composer text and focus. Messages/drafts are
not automatically persisted. Callback errors use the existing plugin error boundary.
Use documented async APIs for agent work, never blocking I/O in a UI callback.

The host permits 8 panels per plugin and 32 overall. Users select panels from the
right-side selector, resize the divider, or hide the area. Publishing updates does
not undo a user's hide choice; `focus_panel` explicitly reveals it. Width,
visibility and selected panel are included in layout presets. Panels are recreated
by enabled plugins at activation; layouts never enable plugins or restart agents.
Unload removes only that plugin's panels and cancels pending panel callbacks.

Declare `[ui] right_panel = true` as optional manifest metadata. No native widget,
PTY handle or private host object is exposed. This extension hosts model-driven
text/input/actions, not embedded interactive CLI terminals; `terminals.run` still
uses the existing terminal tabs. See `examples/plugins/right-panel` for a complete
working example. Existing left-side `ui.sidebar` is unchanged.

### Labelled panel actions

Right-panel rows may set `action_labels: true` alongside `actions`. Core renders
readable buttons in a wrapping row instead of icon-only actions. Use this for
sign-in, task controls and approval decisions whose meaning needs a label. The
field is optional and must be a boolean; existing rows retain their appearance.
Panel transcript updates follow the bottom only when the reader is already near
the bottom, and preserve the unsent composer text. The main toolbar's right-panel
button hides/shows the panel without unloading its plugin.

### Conversation presentation
`ui.supports("panel_conversation")` advertises structured message cards.
`ui.panel(..., messages=[...])` accepts up to 150 messages with unique string `id`,
`role` (`user`, `assistant`, `tool`, `notice`), `text`, optional `title` and `time`.
Keep IDs stable while streaming so expanded tool cards survive updates. Messages
are bounded to 64 KiB each and 256 KiB overall. Assistant text supports escaped
Markdown-style bold, headings, lists and code; HTML is never interpreted. Tool
messages expand in place. `messages=None` retains legacy plain text. Supplying
messages keeps the composer visible even with `on_submit=None`, which disables
Send while preserving the draft. `text` can remain a plain transcript fallback.


#### Composer attachments

`ui.supports("panel_attachments")` advertises `ui.panel(..., accept_attachments=True)`.
This requires `filesystem_read` in addition to normal panel permissions. The composer
supports file selection, file drops and Ctrl+V images. With attachments, `on_submit`
receives `{"text": str, "attachments": [{"name": str, "path": str, "size": int}]}`;
without attachments it still receives a string. Paths identify private local copies,
not remote project paths. Copies remain available until panel teardown, then are
removed; do not persist these paths as durable history attachments.

Limits: eight files per message, 10 MB per file, 30 MB per panel session. Long pasted
text becomes a UTF-8 text attachment without truncation. Return `False` synchronously
to restore a rejected submission when the composer is still empty. Plugins must
handle provider format support and permissions; attaching a file does not make a
text-only model multimodal or authorize execution of file contents.


Conversation messages may include `actions=[(label, callback), ...]` (up to four).
This requires `commands`; `ui.supports("panel_message_actions")` advertises support.
Actions appear below the message and run on a deferred GUI turn through the plugin
lifetime/error guard. Removed cards cannot dispatch queued actions. Keep request
identity checks in the plugin to reject resolved or cancelled approvals.


### Personal plugin test placement
Keep personal plugin unit tests in that plugin's `tests/` directory, outside the
NaUi checkout. Native GUI integration tests belong in
`~/.config/naui/plugin-tests/<plugin-id>/`, separate from the installed plugin:
they may exercise GTK/core internals that are forbidden in plugin runtime code.
Keep their disposable fixtures there too. `/plugins/` and `/plugin-tests/` at the
checkout root are ignored for local development. Tests for NaUi's public APIs and
bundled plugins remain in the main repository.

### Managed subprocesses and SSH command workspaces

`api.processes.json_lines(cwd=..., command=[...])` starts an explicitly named local
subprocess with bounded JSON-line input/output. Requires `command_execution`;
maximum four live processes per plugin. The handle exposes send, request, drain,
incoming queue and close. The plugin supplies protocol semantics. Core unload
closes every owned process and reaps its process group. No provider is selected,
installed, authenticated or contacted by NaUi itself.

`api.devices.command_workspace(device_id=..., cwd=..., existing=None)` requires
command_execution and remote_access. It prepares a private local helper workspace
for a connected SSH device and returns cwd, command argv, device_id and remote_cwd.
The optional existing workspace must be inside the same plugin cache and match
the saved remote host/path. Reconnection refreshes its transport details. This is
an arbitrary-command capability for trusted plugins, not a filesystem sandbox.
`api.devices.connect(id)` opens ordinary SSH authentication and requires
remote_access and terminal_execution.

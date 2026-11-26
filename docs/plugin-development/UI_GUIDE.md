# UI patterns

The Plugins manager opens a main-area tab, not a permanent file-tree panel.
Commands registered through `api.commands.add` appear there. Invoking a command
restores the editor/terminal that was active when the manager was opened, so
current-editor APIs retain context. Explicit file actions appear on the clicked
file's context menu. `api.editor.add_action` registers a command that receives
the current document snapshot (or None).

Use notifications for short status, sidebar panels for compact navigation, and
main-area read-only text/browser views for work content. Public APIs never give
you native GTK widgets. Do not import GTK or manipulate core windows.

```python
api.ui.sidebar("monitor", "MONITOR", [
    {"text": "Ready", "actions": [
        ("Refresh", lambda: api.notifications.show("Refresh requested"))]}])
```

Sidebar rows contain `text`, optional `(label, callback)` actions, and optional
`header=True` for heading actions. Labels ellipsize; complete values have tooltips.
Updating the same plugin-local ID replaces its rows. Each contribution has its
own Activity Rail panel; only one sidebar job is visible. Panels are removed on
unload, with navigation falling back to Explorer. Use `api.ui.focus_sidebar(id)`
to activate a panel and `api.ui.set_sidebar_badge(id, count)` to show attention
without forcing it open. Both require `views`. No native widgets are exposed.
Header actions except Refresh live in the panel overflow menu.

For a main view:

```python
api.ui.open_text("local", "Results", "Read-only output", language=None)
```

For a browser, provide data only: `api.ui.open_browser(device_id, title, refresh,
more)`. Its handle supports rows `[{text, select}]`, filters `[(id, label)]`,
detail text and optional file actions, state, present, stop. Browser limits:
1,000 rows, 512 filters, 500 detail actions, 8 MiB text, 16 owned browser tabs.
Views retain their original device context. Closing a view or disabling its
plugin disconnects callbacks. Owned read-only tabs close on plugin unload;
normal user editor/terminal tabs are unaffected.

Create Plugin with AI collects an ID/name/description/permissions and writes a
small scaffold only. Copy AI Prompt does not contact an AI service or start an
agent. Open Folder pins the directory in the local workspace; Terminal Here opens
a normal Naui shell there. AI prompts are kept in memory, not persisted.

Install Folder/ZIP performs static inspection first, reviews permissions, and
installs disabled. Validate/Enable/Reload require explicit trust before imports
or tests execute. Validation Details and Copy Error for AI support repair.
Reload validates changed code, removes old owned resources, and activates fresh
source without restarting Naui. New permissions require a new review.

Use `api.ui.open_diff` for unified patches. Large diffs, side-by-side rendering,
inline mode, theme colors and lifecycle ownership belong to the host, not the
plugin. Sidebar file rows should use `select` and a compact `menu`, rather than
permanently rendering many action buttons. `children` provides collapsible
repository/status groups with stable `id` values. Full logical paths belong in
tooltips; mounted runtime paths must not appear in normal UI.

For an assistant beside the editor, use `api.ui.panel("chat", "Assistant",
text=transcript, on_submit=send_message)`. This requires `views` and `commands`.
Check `getattr(api.ui, "supports", lambda _: False)("right_panels")` for older
installations. Updates preserve the unsent draft; use `api.ui.focus_panel("chat")`
only for an explicit user action. The host owns resizing, hiding, panel selection,
layout presets and unload cleanup. These panels support model rows, plain text
and a multiline composer, not embedded native widgets or CLI terminals.
See [the reference](API_REFERENCE.md) and `examples/plugins/right-panel`.

Sidebar presentation rows may also include `icon` (theme icon name), `detail`
(muted secondary text), `badge` (trailing status), and `selected` (highlight).
A `primary` callback renders a labelled main action button; `enabled=False`
disables it. Top-level `footer=True` rows stay in a bounded bottom section.
These fields are supported by the sidebar host; right-panel schema limits remain
as documented in the API reference.

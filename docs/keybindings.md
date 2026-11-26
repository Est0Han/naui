# Keybindings

Open **Settings → Keybindings**. Search by command name, ID, category, context
or current shortcut. Click a shortcut, press the new combination, then Save.
Clear Shortcut intentionally unbinds it. The row's reset button restores its
default; Reset All asks before removing all overrides. Changes take effect
immediately. Menus and editor navigation tooltips show current bindings.

Commands remain accessible from their existing buttons and menus. Native text
editing (caret Tab, text-field editing, clipboard operations) remains provided by
GTK. Cleared/remapped Naui bindings for Ctrl+D/F/H/Z also suppress GTK's
hidden text-editing bindings for those keys, so a removed shortcut cannot
unexpectedly delete text or undo changes. Naui's line indentation command handles a selected block on Tab; a remapped
indent command can also indent the current line.

## Context and conflicts

Contexts are `global`, `editor`, `terminal`, `file_tree`, `sidebar`, `git`, and `plugin`.
The actual focused editor/terminal/tree or tool view determines the context;
search, path and rename text fields retain ordinary editing behavior. A matching
enabled command in the focused context takes precedence over Global. Disabled
commands are skipped. Different specific contexts can use the same combination.

A same-context assignment requires **Replace Existing** confirmation, which
clears the old assignment. Capture also reports overlaps with Global bindings.
Duplicate plugin defaults or conflicting hand-edited/imported assignments leave
**all** commands in that context/combination unbound until the user resolves them;
plugin load order never picks a winner. An import with conflicts requires review.

Terminal `Ctrl+C`, `Ctrl+D` and `Ctrl+Z` always reach the PTY, even if assigned to
a Global or Terminal command. Capture warns about this protection. Other terminal
keys are consumed only by an explicitly bound terminal/global UI command.
Editor-only shortcuts never intercept terminal input.

## Profiles

Linux stores overrides in `$XDG_CONFIG_HOME/naui/keybindings.json` (default
`~/.config/naui/keybindings.json`), alongside other Naui user preferences.
The file contains only command IDs and normalized shortcuts, never credentials,
workspace paths or widget identifiers. `null` means intentionally unassigned:

```json
{
  "version": 1,
  "overrides": {
    "editor.toggle_comment": "Ctrl+Shift+/",
    "runner.run_current": "F6",
    "editor.duplicate": null
  }
}
```

Export writes this profile; Import merges it into existing overrides after
validation. Malformed imports leave the current profile intact. Unavailable
command IDs are reported and retained, so profiles work across machines and
plugins can recover their overrides after re-enabling or reinstalling.
Invalid manually edited bindings fall back to defaults with a startup warning.

Modifier order is `Ctrl+Alt+Shift+Super`; aliases include Control, Meta/Cmd and
Primary. Shifted punctuation is normalized to its base key plus Shift (for
example `Ctrl++` becomes `Ctrl+Shift+=`). Modifier-only presses do not bind an
action. Linux defaults are preserved; Primary maps to Super on macOS, whose
native keyboard behavior remains unverified. Multi-stroke chords and presets
are not implemented.

## Plugin commands

Declare the `commands` permission, then use the public API:

```python
api.commands.register(
    id="hello",
    title="Hello",
    callback=lambda: api.notifications.show("Hello"),
    default_shortcut="Ctrl+Alt+H",
    context="global",
)
```

IDs are namespaced under the plugin ID (`example-plugin.hello`). An optional
`enabled` callable controls availability. The default is only a suggestion;
user overrides win. Commands are removed on disable/reload, overrides survive,
and callbacks use existing plugin exception isolation. API v1's `commands.add`
and restricted runner `add_shortcut` remain compatibility adapters.

Git diff tabs use `F7` / `Shift+F7` for next/previous hunk. Source Control file actions are registered as `git.stage`, `git.unstage` and `git.open_diff` without forced defaults; they act on the current file diff.

Sidebar activities are commands: `view.explorer` (Ctrl+Shift+E), `view.search`
(Ctrl+Shift+F), `view.source_control` (Ctrl+Shift+G), `view.transfers`, `view.devices`
and `view.settings` (unbound). `sidebar.focus` is unbound. The `sidebar` context
provides independent zoom defaults; Explorer resolves `file_tree` first, then
`sidebar`, then global commands. Other sidebar panels use sidebar context,
without borrowing the currently open main tab's context. See [sidebar guide](sidebar.md).

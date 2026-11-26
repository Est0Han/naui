# Activity Rail and sidebar

The sidebar shows one primary job at a time. The narrow native GTK activity rail
selects **Explorer**, **Search**, **Source Control**, **Transfers**, **Devices** or
**Settings**. Each panel stays alive in a native stack; switching activities does
not recreate trees, disconnect devices or close main-area tabs.

- **Explorer** fills the available height with lazy device/workspace trees.
  A single device has a folder title and compact connection context. Multiple
  devices retain independent expandable trees. Device management lives in Devices.
  The `+` menu contains New File, New Folder and Add Workspace Root. The overflow
  menu contains root/device actions, Collapse All, Refresh All, Explorer Settings
  and the session-only “ls after cd” preference.
- **Source Control** uses the existing device-aware Git plugin. Repository groups,
  Changes, Staged Changes, Conflicts and Outgoing commits get the full panel height.
  The activity badge counts changed files once, including files changed in both
  the index and working tree. Refresh is visible; History is in the heading menu.
  File actions and repository menus remain available. Diffs/history stay in main tabs.
- **Transfers** hosts the existing queue, progress, cancellation and authentication
  dialogs. Its badge counts unfinished transfers; failures show an error indicator.
  Hidden panels continue receiving updates. Transfers never steal Explorer space.
- **Devices** hosts existing local/SSH device actions. Disconnect ends Naui-owned
  sessions, never powers down a server. Removing a workspace device never deletes
  remote files. Explorer still supports roots on several devices simultaneously.
- **Search** provides [workspace file/content search](search.md), grouped results,
  filters, cancellation and logical device/file/line navigation. Editor Find/Replace
  remains separate.
- **Settings** provides sidebar scale/density and links to the existing editor,
  keybinding and plugin settings views, without duplicating those implementations.

## Paths, size and appearance

Explorer shows the last three logical path components as a breadcrumb, with a
full-path tooltip. Click it (or invoke Focus Path Bar) to edit the full path.
Enter uses the existing terminal-aware directory navigation; Escape cancels.
Navigation readiness rules still apply. SSHFS mount paths are never used as labels.
Long filenames/device names ellipsize; inline fields scroll their own text.
Horizontal scrolling is disabled by native scroller policies, not a reset timer.
Git decorations appear in a narrow column after filenames.

Drag the native divider to resize the whole sidebar (including the rail). The
minimum is 220px and default is 300px; 500–700px is allowed when the window has
room. The sidebar leaves at least 300px for main content and hides transiently
in windows narrower than 600px. Width, selected activity, visibility, scale and
density are stored in the existing XDG `ui.json`, outside the project. Invalid
preferences remain protected until Reset Layout. An old 180px width is clamped.

Ctrl+B toggles visibility without destroying panels. View → Reset Layout restores
Explorer, 300px, 100% scale and Compact density, leaving editor/terminal font
preferences intact. Settings → Sidebar scale supports 80–150%. Compact and
Comfortable share the same widgets, with different row/button padding.

## Commands and plugins

All navigation is configurable under Settings → Keybindings:

| Command | Default |
| --- | --- |
| `view.explorer` | Ctrl+Shift+E |
| `view.search` | Ctrl+Shift+F |
| `view.source_control` | Ctrl+Shift+G |
| `view.transfers`, `view.devices`, `view.settings` | Unbound |
| `sidebar.toggle` | Ctrl+B |
| `sidebar.focus` | Unbound |
| `sidebar.zoom_in` / `sidebar.zoom_in_alt` | Ctrl+= / Ctrl+Shift+= |
| `sidebar.zoom_out` / `sidebar.zoom_reset` | Ctrl+- / Ctrl+0 |

Zoom uses sidebar context when navigation has focus. Explorer commands resolve
before sidebar commands, then global commands. A sidebar text field keeps native
text editing shortcuts; global zoom still targets sidebar appearance there.
Editor/terminal zoom preferences remain independent. Rail tooltips show current
bindings. Buttons remain keyboard focusable with accessible labels.

Public `api.ui.sidebar(id, title, rows)` contributions become independent activity
panels automatically. Reusing the ID updates the same panel, not another widget.
`api.ui.focus_sidebar(id)` activates it; `api.ui.set_sidebar_badge(id, count)` sets
its badge. Neither exposes GTK objects. Plugin unload closes owned callbacks and
removes its panel, falling back to Explorer if needed. An unavailable saved panel
falls back without preventing safe-mode startup. The Git panel keeps a stable
Source Control activity even when the Git plugin is disabled.

## Desktop acceptance

```bash
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/activity_sidebar_smoke.py
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/sidebar_git_smoke.py
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/terminal_tabs_smoke.py
```

The first script uses disposable generic fixtures, a disconnected SSH device and
mock transfer snapshots (no remote connection), verifies panel/scroll/tree retention,
width/scale combinations, path editing, main-area diff retention and mixed-tab
close/GC stress. The other scripts cover inline filename editing and native VTE
surviving-terminal commands. A display is required; exit 77 means untested, not pass.
Real SSH connections, real transfers and rendered theme quality need desktop review.

## Refactor verification and changed files

The previous host stacked Devices, Files and plugin Source Control sections in
the left pane, with Transfers in a permanent bottom pane. The new host keeps
separate persistent pages and shows one selected activity. Filesystem, Git
command transport, PTY ownership, remote execution and transfer workers retain
their existing implementations. The only backend addition is a cheap read-only
filesystem display label, which neither probes executables nor mounts anything.

Files changed for this refactor:

| Area | Files |
| --- | --- |
| Panel host/state/layout | `src/naui/sidebar.py`, `src/naui/sidebar_layout.py`, `src/naui/ui_settings.py`, `src/naui/gui.py` |
| Explorer/device presentation | `src/naui/plugins/base_file_explorer/workspace_sidebar.py`, `src/naui/plugins/base_file_explorer/explorer.py`, `src/naui/workspace.py` |
| Transfers presentation | `src/naui/transfer_ui.py` |
| Commands | `src/naui/keybindings.py`, `src/naui/keybinding_host.py` |
| Plugin/Git integration | `src/naui/plugin_api.py`, `src/naui/plugin_validation.py`, `src/naui/plugin_views.py`, `src/naui/plugins/base_git.py` |
| Editor reveal routing / installation | `src/naui/editor_view.py`, `src/naui/installation.py` |
| Tests | `tests/test_activity_sidebar.py`, `tests/activity_sidebar_smoke.py`, `tests/test_terminal_tabs_layout.py`, `tests/test_shutdown.py`, `tests/test_watch_git_runner.py`, `tests/watch_runner_git_smoke.py`, `tests/sidebar_git_smoke.py` |
| Documentation | `README.md`, `docs/sidebar.md`, `docs/keybindings.md`, `docs/git.md`, `docs/plugin-development/UI_GUIDE.md`, `docs/plugin-development/API_REFERENCE.md` |

Verification for this change:

- Full suite: **464 tests; 459 passed, 5 environment-dependent skips**. Run with
  Python faulthandler and GLib fatal warnings; real disposable Git repositories,
  native headless GLib/GIO/PTY checks and existing installation tests included.
- New sidebar coverage: **19 tests**, including 200 controller panel switches,
  saved selection/width/scale/density, reset, badges, device/logical headers,
  keybinding dispatch, plugin identity/failure handling, native CSS parsing at
  four scales/two densities and mocked native settings-widget interfaces.
- All four public SDK example plugins validated and passed their plugin tests.
- Release hygiene: **184 text/source/assets audited, zero findings**; version
  **0.2.0**. Generated Python caches removed after testing. No commit/push made.
- Activity, sidebar/Git and terminal graphical smoke scripts were attempted and
  each exited **77: no accessible display**. These are skips, not native passes.
  No before/after screenshots could be captured. Real remote connection/transfer
  acceptance and rendered theme/geometry quality remain unverified here.

Known limits: workspace replacement and a Quick Open popup are not implemented. Panel
widgets retain in-session expansion/selection/scroll state; the persisted general
preferences cover active panel, appearance, width and existing section/device
preferences, not every plugin's scroll position or nested group expansion.

# Workspace Search

Select **Search** in the Activity Rail. Search replaces the active sidebar panel;
Explorer remains alive and keeps its expanded folders. Results open files in the
main area. Switching panels preserves the query, options, groups and results.

Choose **Contents** to find text, or **Files / Folders** to match names and logical
paths. File search ranks exact/prefix filename matches first, followed by partial
and fuzzy filename matches and weaker path matches. For example, `tdev` can find
`tests/test_device.py`. Empty folders are included. No file contents are read for
file search.

Content search supports **Aa** (case sensitive), **Ab** (whole word) and **.***
(regular expression). Plain text is the default. Queries are single-line;
multiline matching is not supported. Invalid regex syntax appears in the panel.
Ripgrep uses its own regex engine; compatibility mode uses Python's regex engine,
so advanced dialect-specific constructs may differ. Matches in a file are grouped
and collapsible, with line numbers and bounded previews. Each match counts once,
even when several matches share a line. Double-check results after external edits:
search results are snapshots, not live edits.

The scope selector searches **Workspace**, **Current Root**, **Device**, or
**Selected Folder**. Workspace searches only configured/pinned roots. It does not
implicitly search a whole remote filesystem. Root paths are logical device paths;
identical paths on different devices remain separate results. Nested pinned roots
are searched once. A folder selection is taken from the current device's Explorer.

## Filters

Include/exclude fields accept comma-separated positive globs:

- `*.py,*.toml` matches basenames anywhere in a root.
- `src/**` matches paths recursively beneath `src`.
- `src/*` matches one path component, not deeper descendants.
- `?` and bracket character classes work inside a component.

Exclusions take precedence over includes. Negated include patterns, brace expansion and literal
commas in patterns are not supported. Default exclusions prune `.git`,
`node_modules`, `__pycache__`, `.venv`, `venv`, `build`, `dist` and `.cache`.
Remove a default exclusion explicitly to search that directory. **Include ignored
files** bypasses Git ignores, but does not bypass explicit exclusions. **Include
hidden files** enables dotfiles independently. Symlinks are not followed, including
symlinked files, to avoid cycles and leaving the intended workspace.

Ripgrep respects `.gitignore`, `.git/info/exclude` and global ignores by default.
Compatibility traversal asks Git `check-ignore` in batches when a root is in a
repository, including global/repository rules. Like ripgrep, ignore patterns apply even to matching tracked files.
If Git is unavailable, explicit filters still apply and the panel reports that
ignore rules could not be consulted. Non-Git folders still work. Git ignore rules
inside a nested repository may depend on Git's parent-repository behavior;
configure a separate root for a nested repository when needed.

## Local and remote execution

Content search prefers `rg --json` when ripgrep is installed. Its CLI config is
disabled so unrelated preprocessing/follow flags cannot override Search options. Naui detects it using
PATH. For SSH devices it executes ripgrep **on the remote machine** through the
existing authenticated OpenSSH transport; it never runs remote search against a
local SSHFS `.git` directory. Remote ripgrep availability is cached by device and
connection generation. Naui neither installs tools nor requests sudo.

Without ripgrep, bounded metadata traversal and reads use Naui's FileBackend and
binary streams. This works with local and SSH compatibility devices without remote
Python or additional tools. For remote compatibility search, Naui deliberately
uses cancellable SSH filesystem streams, rather than recursive reads through a
potentially stale FUSE mount. SSHFS remains available to the normal editor when a
result is opened. Compatibility mode is slower, especially over SSH.

Compatibility reads skip binary/invalid UTF-8 data and files above **10 MiB**.
At most one file is temporarily spooled (small files stay in memory); that temporary
snapshot is deleted immediately. Regex matching is isolated in an owned local
process so pathological patterns can be cancelled. There is no persistent index
and no remote project mirror.

File search enumerates metadata only. A bounded in-memory path cache (20,000 entries
per root, 40,000 overall) accelerates repeated queries for up to five seconds.
Existing file-watch events and workspace/device changes invalidate it. Unwatched
external changes may remain stale until that short expiry. Naui does not create
recursive watchers for search or store paths/results/queries on disk.

## Progress, cancellation and limits

Typing debounces for **220 ms**; Enter starts immediately. Starting a new query
cancels the old one immediately. Results stream into a bounded queue and GTK
updates in small batches. The list uses GTK's virtualized tree/list model; it does
not construct one complex widget per match. Worker threads receive only plain
data and filesystem objects, never GTK widgets.

Cancel stops the current search; Clear cancels and resets it. Escape cancels when
Search has focus. Results already returned remain visible and the search is marked
incomplete. If a device disconnects, the active search is cancelled and existing
results remain; reconnect before opening remote results. A disconnected device's
root reports an error without preventing other roots from being searched.

Defaults cap search at **10,000 matches**, **100,000 enumerated metadata entries**
for compatibility/file search, **10,000 queued directories**, and **five minutes**. The service accepts smaller
limits and a file-size limit up to 100 MiB; the first UI uses the defaults. Once a
cap is reached, refine the query or scope. Ripgrep records above 1 MiB (for example,
a giant minified line) stop that root with a concise warning rather than allocating
unbounded previews. Output and warnings are bounded. Permission errors continue
with a limited warning summary. Broader warnings are available in the status tooltip.

Search processes have explicit ownership. Cancellation closes only the owned SSH
channel; its remote EOF guardian terminates the command that channel launched.
No `pkill` or global process cleanup is used. Normal application shutdown cancels
search and joins its workers before releasing shared authentication resources.

## Navigation and commands

Click a content match to open its logical device/file through the existing editor
pipeline and select the match. Already-open buffers keep unsaved edits and are
reused. Paths and offsets preserve Unicode; GTK selection uses character offsets,
not ripgrep byte offsets. File results use normal viewer selection; folders reveal
in Explorer. Arrow keys and Enter navigate/activate the native result list.

Right-click for Open, Reveal in Explorer, Copy Path or Copy Relative Path.
Copy Path includes device identity and never includes internal SSHFS mount paths.

Configurable commands:

| Command | Default |
| --- | --- |
| `view.search` (show/focus content search) | Ctrl+Shift+F |
| `search.files` (file/folder search) | Ctrl+P |
| `search.workspace` | Unbound; alias for content search |
| `search.focus` | Unbound |
| `search.cancel` (Search context only) | Escape |
| `search.next_result`, `search.previous_result`, `search.open_result` | Unbound; native arrows/Enter also work |

Terminal Ctrl+C/Ctrl+D/Ctrl+Z behavior is unchanged. Editor Find/Replace remains
separate. Workspace replacement, persisted search history, Quick Open popup and
Git result decorations are left for follow-up work; there is no blind bulk replace.

## Verification

Run the backend/model/regression tests with:

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_workspace_search.py'
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_search_ui.py'
```

Desktop acceptance (requires a real display):

```sh
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/search_smoke.py
```

The smoke script uses a disposable local repository, streamed content/file results,
invalid regex, cancellation, panel switching and editor match selection. A missing
display reports skip/exit 77; it does not prove native rendering or lifecycle
stability. Mock SSH tests exercise safe quoted transport, JSON parsing, fallback,
EOF cancellation and device identity without requiring a live SSH host. Real SSH,
SSHFS/FUSE behavior and desktop rendering still require acceptance on an available
host/display.

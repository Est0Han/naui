# Naui

Naui is a native desktop terminal, graphical file editor and media viewer. Run
`nano file.py` inside its real shell to open a dark editor beside the terminal.
Browse local or SSH files in a synchronized sidebar. All Naui code stays local:
servers need only their existing OpenSSH, Bash and GNU coreutils.

**Linux: supported target. macOS: planned, not built or tested.** This release
retains Python 3, GTK4, VTE GTK4 and GtkSourceView 5. See
[portability and packaging](../packaging/macos/README.md) for the toolkit decision.

## Features

* Real PTY shell, interactive applications, job control, sudo and system OpenSSH.
* Session-only nano interception; `/usr/bin/nano` remains available.
* Dark text/code editor, syntax highlighting, line numbers, current line,
  undo/redo, Ctrl+S, Ctrl+F, dirty markers and unsaved-close confirmation.
* Lazy file tree, path bar, Back/Forward/Parent/Refresh and local/remote identity.
  Expansion only lists contents; navigation and manual cd synchronize by OSC.
* Simultaneous local/SSH workspace panels, per-device terminal state, pinned
  roots, inline creation/rename, reconnection and safe workspace persistence.
* Optional local Pyright LSP: definition, references, hover, diagnostics,
  reviewed symbol rename, device-owned code history and clickable file paths.
* PNG/JPEG/WebP/GIF/BMP preview: fit, 100%, zoom, scroll, dimensions and view-only
  rotation, subject to installed image loaders. GIF uses its first frame.
* MP3/WAV/OGG/FLAC/M4A audio and MP4/WebM/MKV/MOV video: play/pause, seek,
  current time/duration and volume via system GTK media codecs. No autoplay.
* Read-only EXE/DLL inspection: PE architecture, .NET header detection, sections,
  embedded strings and raw bytes. Optional Ghidra integration adds reconstructed
  C-like pseudocode via **Decompile to C-like code**. Files are never executed.
  Preview: first 100 functions, 90-second analysis budget, four-minute total limit;
  this is not exact original source. Set `NAUI_GHIDRA_HOME` for a custom Ghidra
  installation; Ghidra 12.2 needs a Java 25 JDK (`NAUI_JAVA_HOME` can select it).
  Strings scan the first 8 MiB; raw preview shows 4096 bytes.
* Optional [RPF archive browsing](../docs/rpf-support.md) through OpenRAGE, with
  editable extracted text/XML copies and a bounded raw-byte preview. Archive
  rebuilding is not supported.
* Optional lazy SSHFS roots for remote browsing, direct media and local LSP;
  protected writes and transfers retain the existing SSH compatibility backend.
* About dialog, version command, Linux user installer and placeholder app icon.

## Linux installation

Requires Python 3.10+, PyGObject, GTK4, **VTE GTK4 (GI 3.91, not GTK3 VTE 2.91)**,
GtkSourceView 5, GdkPixbuf and OpenSSH. Media uses GTK's installed media backend
(normally GStreamer). No codecs or web server are bundled.

```bash
git clone https://github.com/Est0Han/naui.git
cd naui
./install.sh
naui
```

The installer checks dependencies. On Debian/Ubuntu or Fedora it can install
system dependencies using the user's normal sudo interaction:

```bash
./install.sh --install-deps --python /usr/bin/python3
```

**Optional/recommended:** SSHFS and usable FUSE for integrated remote read access.
Install them explicitly with your distribution's package manager (for example
`sudo apt install sshfs` or `sudo dnf install fuse-sshfs`). Naui never installs
SSHFS automatically. If missing, blocked by container permissions, or unable to
mount, Naui keeps using its existing OpenSSH/Bash compatibility file backend.

Debian 12+/Ubuntu 24.04+ dependencies can also be installed explicitly:

```bash
sudo apt update
sudo apt install git python3 python3-gi gir1.2-gtk-4.0 gir1.2-vte-3.91 gir1.2-gtksource-5 gir1.2-gdkpixbuf-2.0 webp-pixbuf-loader openssh-client libgtk-4-media-gstreamer gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav
./install.sh --python /usr/bin/python3
```

Fedora base packages:

```bash
sudo dnf install git python3 python3-gobject gtk4 vte291-gtk4 gtksourceview5 gdk-pixbuf2 webp-pixbuf-loader openssh-clients gstreamer1-plugins-base gstreamer1-plugins-good
./install.sh --python /usr/bin/python3
```

Additional codecs depend on distro repositories. Image support similarly
requires system GdkPixbuf loaders, including
[webp-pixbuf-loader](https://packages.debian.org/bookworm/webp-pixbuf-loader)
on distributions where WebP is not built in. Decoder errors appear in the preview tab;
the terminal/editor stay available.

Installed paths:

```text
~/.local/bin/naui
~/.local/share/naui/
~/.local/share/applications/naui.desktop
~/.local/share/icons/hicolor/scalable/apps/naui.svg
```

The desktop entry uses the actual absolute launcher path, accepts local files,
and appears in normal Linux menus even when the desktop PATH differs. Available
desktop/icon database tools refresh their caches. If your current shell lacks
`~/.local/bin` on PATH, start a new login session or run:

```bash
export PATH="$HOME/.local/bin:$PATH"
naui --version
```

Reinstallation upgrades only manifest-owned unchanged files. It refuses to
replace unrelated or modified files. `--prefix /custom/user/prefix` derives all
paths from that prefix. Resources are copied, so the checkout can be moved or
removed afterwards. `--skip-dependency-check` is for packaging/testing.

Uninstall from the checkout or installed resources:

```bash
./scripts/uninstall.sh
# Also works after removing the checkout:
~/.local/share/naui/uninstall.sh
```

For a custom prefix use `--prefix /custom/user/prefix` with the checkout script.
Uninstall removes only manifest-listed unchanged files and empty app-resource
directories. Modified files, user configuration/cache and unrelated files are
preserved. Installation itself needs no root; dependency installation is an
explicit option. macOS installation exits with a planned-support message.

## Use

```bash
naui
naui file.py
naui photo.png
naui --version
```

Inside Naui:

```bash
echo hello
pwd
nano /tmp/naui-test.txt
```

Enter `Hello from Naui` and Ctrl+S to save. The terminal remains usable.
`nano one.py two.conf` opens multiple editors. Tree double-click chooses a viewer;
`nano` and **Open in Editor** explicitly request text editing. Unknown binary
files get an unsupported-viewer message, never an external executable. Ctrl+S/F/Z
apply to editor tabs; terminal Ctrl+C/Z/S retain PTY meanings and Ctrl+Shift+C/V
copy/paste. About shows version/license and the configured real project URL.
The compact toolbar shows action names and configured shortcuts on hover.
Settings contains Editor, Keybindings, Plugins and About; selection accents mark
the active sidebar view and tab without changing their keyboard shortcuts.

With two or more tabs open, choose **View → Split Side by Side** or
**Split Top and Bottom**. The current tab moves into a second group; drag the
divider to resize the groups. Click a tab or its contents to make that group
active. Save, Find and terminal shortcuts apply to the active group.
**Move Tab to Other Group** moves the current tab, and **Single View** reunites
all tabs without closing files or terminals. An empty group closes automatically.
These actions can also be assigned shortcuts in Settings → Keybindings. Splits
last for the current session and support two groups of distinct tabs; they do not
duplicate the same document into two views.

Directories expand through filesystem backends without sending terminal input.
Double-click navigates the shell. The path bar accepts absolute/relative paths,
`~`/`~/...`, spaces, quotes and Unicode. Shell metacharacters, `$HOME`, globs and
command substitutions are literal input; `~other-user` is not implemented.
Back/Forward histories are separate per device and pinned root. Run ls after navigation is a
session-only option, off by default; tree listings never parse ls.

Right-click provides Open, Open in Editor, Open Terminal Here, Copy Path,
New File/Folder, Rename, Delete, Refresh and Properties. Open Terminal Here
activates that device's terminal and navigates it. Creation and rename use inline
name input: Enter confirms, Escape cancels. No file is created until confirmation;
failures preserve the name and original item. Delete shows the device/name/full
path, requires confirmation and rejects nonempty directories. Rename refuses
overwrites. Close affected editor tabs before rename/delete. Single-click selects;
double-click opens persistent tabs.

## Multi-device workspace

The FILES sidebar retains a separate expandable panel for the local computer
and every registered SSH device. Entering SSH does not remove the local tree.
Click a device name to activate its terminal; clicking a disconnected SSH device
reconnects through OpenSSH and its normal terminal authentication prompts.
Icons and text distinguish connected/connecting/disconnected states. Each panel
has its own path bar, browsing state, roots and lazy filesystem tree.

Use the arrow beside a device to collapse its entire card to one row; this choice
is remembered. Collapsed cards form a compact group at the bottom of Explorer.
Disconnected devices are hidden by default and remain available under Devices.
In **Explorer → …**, turn off **Hide disconnected devices** to show their cards.
**Maximum tree height** adjusts the space used by
each file tree (40–600px); short trees shrink to their contents. These controls
only affect presentation and never disconnect a device or remove a workspace.

* **+ Device** adds an SSH alias/user@host with an optional display name, or pins
  a local folder on the local device. Simple Host entries from ~/.ssh/config are
  offered as suggestions; Includes, wildcard matching and authentication remain
  OpenSSH's responsibility. No keys are read by Naui's alias suggester.
* **+ Root** pins a folder on that device. The terminal's current directory also
  remains visible, even outside pinned roots. **Remove from Workspace** only
  unpins the reference: it never deletes the actual folder. The current-directory
  view remains if the terminal is still there.
* **+ File / + Folder** target the selected folder (or the selected file's parent).
  Expansion only lists files. Navigating switches to the matching terminal and
  uses the existing acknowledged, shell-quoted cd protocol.
* Device switching retains editors, terminal processes, current directory,
  Back/Forward history and expanded folders. Within a pinned root, history is
  scoped to the most specific root; outside roots, device history is used.
* Text/media tabs retain device_id, absolute path and the same backend object.
  Editor labels show LOCAL or the device name. Ctrl+S never uses the sidebar's
  current device to choose a destination. Reconnect restores that backend's SSH
  transport and dirty buffers; original revision checks still apply. A save
  interrupted by reconnect remains unconfirmed/dirty, rather than reported saved.

SSH added through the sidebar gets a dedicated real PTY. Manually running ssh
in a local terminal registers a remote device and retains the suspended local
shell. Selecting LOCAL can create another local terminal while that SSH shell
continues. A device stores a list of terminal sessions; its tree follows the
selected one. Multiple SSH terminals can belong to the same device. Each owns
its own temporary local OpenSSH control socket; filesystem access uses a live
transport for that same device. Closing one terminal releases only its transport
and switches to a surviving session. Late exits cannot disconnect a replacement.
Remote reconnect attempts restore the previous directory through the same shell
readiness handshake, without taking focus from another device/editor.

Workspace metadata is atomically saved with mode 0600 under
$XDG_CONFIG_HOME/naui/workspace.json (default ~/.config/naui/workspace.json).
It contains only device IDs/names, host aliases, optional username/port, pinned
roots and device ordering. Runtime sockets, full SSH options, passwords, keys,
terminal content and authentication state are excluded. Loaded SSH devices start
disconnected and never connect automatically. Navigation/expansion state lasts
for the application session; only pins/device metadata persist across runs.
Uninstall preserves this user configuration. Invalid existing config is reported
and preserved until an explicit workspace change.

Core TransferManager handles binary cross-device copying through backend streams;
the tree only requests device-aware jobs. Registry/device/backend objects remain
independent of GTK. GUI actions are disabled while disconnected or an inline operation
is pending. At most 32 devices and 32 pinned roots per device are accepted.

## Bundled plugins, transfers and terminal lifecycle

Core owns devices, PTYs, authentication, file backends, TransferManager, plugin
permissions and shutdown. Default-enabled bundled plugins register the preserved
explorer, filepath navigation, generic LSP, Pyright and media functionality.
Bundled `base-runner` and `base-git` use the public terminal, command, watch and
view APIs; core owns transport/process lifetime and filesystem event delivery.
The Plugins main tab identifies bundled/user plugins and offers plugin commands.
The LSP and filepath implementations now reside under `src/naui/plugins/`;
compatibility modules retain existing import paths. Existing GTK adapters still
use a restricted bundled host hook; this is an incremental extraction, not a
complete arbitrary frontend-extension API.

User plugins live in `$XDG_CONFIG_HOME/naui/plugins/<plugin-id>/` (default
`~/.config/naui/plugins/`). In **Plugins**, use **Add Plugin…** to install a folder
or ZIP. Your plugins appear first with an Enable/Disable button. The per-plugin
menu holds maintenance actions, and **Details** opens
permissions, validation results and logs separately. **Create & develop** groups AI creation,
agent settings and documentation. Built-in features and plugin commands are in
collapsible sections; AI activity is collapsed by default and shows only applicable task actions.
Bundled adapters are essential and remain managed by Naui.

Create with AI writes `plugin.toml`, `plugin.py`, `README.md` and a small unittest
scaffold. Copy AI Prompt includes the actual installed SDK/example paths, your
description/permissions, the assigned-directory restriction and the exact
`PLUGIN_API_CAPABILITY_MISSING` rule. Manual copy mode remains available.
**AI Agents** detects Codex/Claude on PATH and configures custom CLI agents.
Choose a provider and optionally a model ID, then explicitly **Start AI Agent**
in the plugin's own real PTY. Review the main-area **AI Changes** diff, then
Validate → Enable or Reload. Copy Error for AI, Send Fix to Agent and AI
Modification Prompt support the next iteration. No paid task starts automatically.
See the [AI Agent guide](../docs/plugin-development/AI_AGENTS.md) for prompt delivery,
provider authentication, task ownership and limitations. API providers are a
planned framework; no live API adapter or token storage is claimed.

```bash
./naui --dev-plugins
./naui plugin validate examples/plugins/hello-world --static  # no code execution
./naui plugin validate examples/plugins/hello-world           # trusted imports/tests
./naui plugin api-version
./naui plugin sdk-path
./naui plugin paths
./naui plugin list
./naui plugin enable hello-test --trust     # review/trust imports/tests and permissions
./naui plugin disable hello-test           # next launch; use GUI for immediate disable
./naui plugin uninstall hello-test --yes    # close Naui first; settings retained
./naui --safe-mode                         # this launch: bundled only; preferences intact
```

Plugins import the public **naui_plugin_api** package. See the concise
[agent instructions](../docs/plugin-development/AGENT_INSTRUCTIONS.md),
[API reference](../docs/plugin-development/API_REFERENCE.md),
[UI guide](../docs/plugin-development/UI_GUIDE.md),
[manifest](../docs/plugin-development/MANIFEST.md) and
[security rules](../docs/plugin-development/SECURITY.md). Small examples live
in `examples/plugins/{hello-world,sidebar-panel,right-panel,file-action,terminal-command}`.
Plugins can create resizable right-side panels with text, actions and a multiline
message box through `api.ui.panel`. Width, visibility and selection are remembered
and included in View layout presets. See the `right-panel` example; existing
API v1 plugins remain compatible.
Python 3.10 needs `tomli` for TOML; Python 3.11+ uses the standard library.

Folder/ZIP installation performs static inspection, shows permissions and
installs **disabled**. Import/test validation executes reviewed Python in a
bounded child interpreter (30 seconds / 1 MiB output), not a security sandbox.
Live plugins are trusted in-process Python. API calls check declared capability
and device identity; no authentication objects or credentials are exposed.
Changed code is not silently auto-enabled after restarting Naui: saved trust
includes a content fingerprint. Failed/interrupted startup plugins are held for
explicit recovery. Settings and reviewed grants persist; passwords do not.
AI prompts use private temporary runtime files, removed after task termination;
they are not saved in durable Naui settings/logs. Agent-owned history is separate.
Logs are bounded and session-only. CLI recovery changes next-launch state;
close Naui before CLI changes to avoid concurrent settings writes.

Reload uses fresh module namespaces/source (including relative helpers), removes
API-owned commands/views/watchers/timers, and preserves user terminals/editors.
Invalid reload leaves the previous working plugin active. A newly requested
permission needs a fresh review. Native widgets stay core-owned. SDK validation
is an API-boundary check, not a malware scanner. Arbitrary native widget plugins,
Git-URL installs, live-plugin process isolation and long-running background-task
APIs are not implemented. Legacy plugin.json/start(api) remains compatible;
`./naui --plugin /absolute/folder` still provides session-only explicit loading.

## Live filesystem, runner and Source Control

The resizable **left** sidebar has an Activity Rail: Explorer, Search, Source
Control, Transfers, Devices and Settings. Only the selected panel is visible;
other panels remain alive with their tree/list state. Editors, terminals, media,
Git diffs/history and plugin tools remain in the main pane. [Workspace Search](../docs/search.md)
finds files/folders or text across configured local/SSH roots, with filters,
streamed results and cancellation; editor Find/Replace remains separate.

Explorer retains multiple local/SSH trees, compact workspace context, creation
and overflow menus, vertical-only scrolling, filename ellipsis and trailing Git
decorations. Click the breadcrumb to edit a logical path; Enter navigates and
Escape cancels. Transfers and device connection management have dedicated panels.
Source Control's activity badge counts changed files; Transfers shows active work
and failures without occupying Explorer space.

The native divider supports a 220px minimum, 300px default and wider panels when
the window permits. Width, activity, visibility, independent sidebar scale
(80–150%) and Compact/Comfortable density are saved in XDG `ui.json`. Ctrl+B
toggles the sidebar. Configurable Ctrl+Shift+E/F/G open Explorer/Search/Source
Control. Ctrl++/Ctrl+-/Ctrl+0 scale navigation when it has focus, without changing
editor/terminal font preferences. Settings links existing settings views; View →
Reset Layout restores a usable sidebar. See [sidebar guide](../docs/sidebar.md).

Core `FileWatchService` shares subscriptions between explorers, editors and Git.
Local directories use [GIO native monitors](https://docs.gtk.org/gio/method.File.monitor_directory.html)
(Linux inotify), never repeated `ls`. Live tree updates reload the affected loaded
directory and retain existing expanded nodes and selection where possible.
Roots/expanded directories and parents of open text files are watched. Collapsed
subtrees are not recursively scanned; files there refresh when expanded.

Remote watches inspect only subscribed directories through RemoteFileBackend.
Up to four optional SSH `inotifywait` channels per device send filename-free wake
hints; no remote package is installed. A transient parent shell reads SSH stdin
and terminates/reaps its watcher child on EOF. Missing inotifywait or unusable
key/control-master authentication for hints falls back to snapshots. Polling adapts
from approximately 3 to 15 seconds while idle; hints debounce before snapshots.
Two background scan workers and a 256-directory overall subscription limit bound
work. Snapshot polling uses one nonrecursive, machine-readable GNU find per
directory rather than spawning stat for each child. Symlink directories and recursive watch requests are refused. Disconnect,
root removal and shutdown release watches. Live SSH behavior still requires
desktop/network acceptance outside the execution sandbox.

External text changes never replace buffers automatically. Clean tabs offer
Reload/View Changes; dirty tabs retain editor and external text and offer Compare,
confirmed Reload or confirmed Keep My Version. Keeping adopts the observed disk
revision; a later save still refuses another intervening external edit. Reliable
file rename events update tab/document paths and LSP ownership without changing
devices. Deleted files retain their text and offer Save As (currently a local
copy) or Close. Whole-directory/root rename tracking and merge editing are future
work. Changes while disconnected are checked again on reconnect.

**Run is explicit**: file context menus provide Run, Run in New Terminal and Run
With Arguments; Ctrl+F5 runs the active editor's saved file. Dirty editors ask
before running the saved version. `.py` uses python3, `.sh` bash, `.js` node;
other regular files with executable permission run directly. The originating
document/device and parent directory are retained. Missing interpreters produce
a terminal message. Arguments use shlex parsing then independent shell quoting,
including printable ANSI-C encoding of control characters; they are not retained
in Naui settings. Bash runner command lines are omitted from shell history through
session-only readline integration; ordinary command history is retained. Existing
zsh integration/history handling remains unvalidated here.

Runner output stays in a real PTY and retains device-aware Ctrl+Click path links.
Shift+F5 sends Ctrl+C only to the still-active owned run. Busy terminals are refused
rather than receiving injected commands. Local and SSH new terminals are
supported; remote runs activate/connect the originating device. Each remote
terminal owns an independent SSH session; opening files and filesystem events
never run files.

`base-git` uses the installed Git on each device. Repository roots are cached per
device/path; [porcelain v2 NUL-delimited status](https://git-scm.com/docs/git-status#_porcelain_format_version_2)
handles spaces, Unicode and renames. Multiple repository states remain separate.
Source Control has a compact repository picker with branch names. Selecting a
repository shows its commit message field, staged-only Commit button, and Changes,
Staged Changes and Conflicts. File rows separate the filename, muted folder and
status badge. The bottom Graph section shows the eight latest commits on HEAD
with parent-lane hints; clicking one opens full history. Each repository retains
its own draft and status, including outgoing/incoming counts.
Tree labels retain status decorations for existing entries and aggregate changes.
Deleted paths remain available even though they no longer exist in the tree.
Click a file for a read-only **Side by Side** diff in the main area, with an
**Unified** toggle, original/new line numbers, change navigation and Open File.
Diffs use a black background with full-line highlights: green `+` for additions,
red `-` for removals, and amber `~-` / `~+` for before/after lines paired within a
replacement block. A summary counts these added, removed and modified lines.
Unstaged diffs compare index to working tree; staged diffs compare HEAD to index;
untracked UTF-8 files compare against empty text. Binary changes show metadata.
Large diffs require explicit rendering; command output/render limits protect UI
responsiveness. Historical and AI review patches share the same viewer.

Use **+** beside a file to stage it and **−** to unstage it. The matching icons
on Changes and Staged Changes stage or unstage the whole repository. Then use
the commit message and Commit button to commit staged content.
File context menus also support Stage, Unstage, Open File, Copy Path, Reveal and
confirmed Discard. Repository menus offer Stage All, Unstage All, Fetch, Pull
(fast-forward only) and Push. Commit messages commit **only staged content**;
failed operations preserve drafts and show Git errors. A clean working tree may
still have outgoing commits. No automatic fetch or push occurs. SSH Git executes
remotely even when file access uses SSHFS. See [the Git guide](../docs/git.md) for
operation semantics, Git requirements, safety checks and limits.

**Git History:** click the History icon in the permanent SOURCE CONTROL header
(available even when collapsed), or choose Plugins → Git History. The adjacent
Refresh icon retries detection and status. Loading, Git errors, non-repository
directories and SSH disconnects have visible states rather than disappearing.

History opens in a main-area tab labelled with its device and repository path.
It lists hash, subject, author, UTC date, HEAD/branch/tag decorations and simple
parent lanes. Select a commit for its full message, author, date, parents and
changed files; click a changed file to open its commit diff. Merge diffs compare
against the first parent. The filter includes All branches, HEAD, local/remote
branches and tags; no fetch or checkout occurs. Refresh reloads refs/history.

History uses [explicit Git log fields](https://git-scm.com/docs/git-log) and
[for-each-ref](https://git-scm.com/docs/git-for-each-ref), with bounded async
device commands. The initial page has 100 commits; Load More adds 100 up to
1,000 per tab, with at most 500 ref filters and 500 displayed files per commit.
Remote commands run on the original SSH device without downloading its repository.
Disconnected tabs keep their history and disable actions until reconnect.
HEAD/ref changes debounce history refresh; ordinary file edits refresh status only.
The graph is deliberately simple: explicit parent IDs remain available in details.

Shared filesystem events debounce Git refresh for 500 ms. Git metadata/refs are
also watched so checkout/commit updates do not require parsing terminal commands.
Repository removal/disconnect suspends watches; reconnect refreshes status.
Only watched directories generate live refresh: changes deep in a collapsed
subtree may require expanding it or the Refresh Source Control command. At most
24 repositories and 20,000 changes per status are supported; long sidebar lists
are capped while tree decorations retain all parsed status entries. Git pager,
fsmonitor hooks, external diff and textconv are disabled for status/diff viewing.

Drag one regular file or directory from a device tree onto a directory in another
(or the same) tree. This creates a **COPY** job in the Transfers panel. Local→local,
local→SSH, SSH→local and SSH A→SSH B use the same engine. SSH→SSH bytes stream
through Naui's local process in 64 KiB chunks with backpressure; neither remote
host authenticates to the other and no complete local transfer cache is used.
Use connected device trees; reconnect a disconnected device before dropping.
External desktop-file-manager drops and multi-selection drops are not yet supported.

The panel shows devices/paths, bytes, state, mean speed, discovered totals and ETA
when known, with working cooperative Pause/Resume, Cancel and Retry controls.
Folders enumerate progressively; totals remain unknown until enumeration finishes.
Symlinks/special files are skipped rather than followed. Depth is capped at 128.
The queue retains at most about 512 jobs and can clear finished jobs; old finished
IDs expire. Sources remain intact; Move is not implemented. Conflicts require Cancel, Keep
Both or explicit Replace File; directory conflicts offer Merge, never recursive
replacement. Keep Both naming lives in backend code and publication refuses races.
A merged folder can have further file conflicts; choices apply individually.

Files stage in the destination directory and publish only after the exact source
byte count and source revision checks. A remote writer also requires explicit
COMMIT, so EOF/cancellation cannot replace the destination with partial bytes.
Cancellation leaves already completed files and folders in place. Normal cleanup
removes owned partials; abrupt server/machine failures may leave partial files.
No blind stale-file glob deletion occurs. File modes/ownership are preserved
where possible; newly created folders are private (0700); ACLs/xattrs and hard-link identities are not copied. Existing
optimistic replacement has a small revision-check/rename race.

Interactive SSH still authenticates through OpenSSH's PTY. Backend/transfer
operations reuse its temporary master when available, prefer key/agent/config
authentication, and can request a core-owned **masked** OpenSSH askpass dialog.
Remembering is optional, device/prompt scoped and memory-only. Cancel cancels the
requesting transfer; disconnect forgets that device's values and exit discards all
session memory. Host-key confirmation requires explicit approval. The short-lived
helper and private uid/nonce-checked Unix socket are entirely local, under a private
runtime directory. IPC nonces live in private temporary files, outside the SSH
environment even with SendEnv *. No passwords enter argv, environment, workspace JSON or logs.
There is no persistent keyring implementation or secure-memory-zeroing guarantee.

Each SSH device has an explicit Disconnect control beside its header and in the
main toolbar. It stops only that device's active SSH jobs/transfers, closes its owned
transport, retains roots and unsaved editors, and leaves other devices/Naui open.
Reconnect by clicking the disconnected device. A nested SSH wrapper is stopped
without terminating its outer local PTY; dedicated SSH tabs close. A changed
foreground job is left alone with an explicit status message. Disconnect never
powers off a host.

Remote exit/Ctrl+D ends only that SSH terminal/session. In a nested SSH command,
normal local-shell execution resumes. A final local shell's normal exit/Ctrl+D
shows **Exit Naui?**: Y exits through normal dirty-buffer/active-transfer checks;
N/Escape restarts locally. Nonzero/signal exits instead offer Restart/Close Tab.
If other terminals remain, only the exiting tab closes. Exit status cannot prove
whether a user literally entered exit; status zero is treated as a normal exit.
`+ Terminal` creates additional local PTYs; SSH still supports one live transport
lease per device. Terminal tab close warns when a foreground program is detected.
Application close checks dirty saves/active transfers and stops plugins, prompts,
workers, OpenSSH and PTYs before normal GTK destruction.

## Code navigation (Python / Pyright)

For multiline editing, comments, line moves, find/replace, editor preferences
and large-file handling, see [Programming editor](../docs/editor.md).

Install Pyright **on the local computer only**. It is optional: the terminal,
filesystem explorer and editor continue working without it. With Node.js/npm
installed:

```bash
npm install --global --prefix "$HOME/.local" pyright
export PATH="$HOME/.local/bin:$PATH"
./install.sh
naui
```

Open a .py/.pyi file to start its local `pyright-langserver --stdio` process.
Code controls appear in editor tabs:

| Action | Shortcut / behavior |
| --- | --- |
| Go to definition | F12 or Ctrl+Click a symbol |
| Find references | Shift+F12; click a result to navigate |
| Hover information | Pause over a symbol, or Ctrl+K / Hover |
| Diagnostics | Underlines and Problems, with clickable locations |
| Rename symbol | F2; enter a name, review the change excerpt, apply to buffers |
| Code Back / Forward | Alt+Left / Alt+Right; separate from filesystem history |
| Open a file path | Ctrl+Click in an editor or terminal |
| Reindex | Refresh remote sources or retry a failed/unavailable language server |

Rename synchronizes captured open-buffer versions and validates every edit
before application. It opens affected files as dirty tabs, supports undo per
tab, and **does not save files automatically**. Ctrl+S uses each document's
original backend/revision. If a buffer changes, a tab closes or SSH reconnects
after preview, the entire pending rename is rejected. File create/delete/move
operations and edits outside the analyzed project are rejected.

Code history and all language-server results carry device_id plus absolute path
and UTF-16 position. Matching pathnames on different machines stay distinct.
Links support relative/absolute paths, ~/paths, quoted paths with spaces/Unicode,
file:/// URIs, path:line:column and Python tracebacks. They only read files;
missing paths report an error instead of creating a file. Terminal links resolve
against **that terminal's current device/cwd**, including background terminals;
old scrollback is not annotated with its historical directory/device. HTTP/SSH
URLs are not interpreted as filesystem paths or device selectors.
Media links reuse the existing viewer registry rather than opening a binary
file in the text editor.

Local analysis uses the nearest project marker inside a matching pinned root,
or the document directory when no root matches. Pin each remote project as a
root to define its analysis boundary. With SSHFS, the local language server reads
the mounted root and maps definition/rename URIs back to the original device.
Diagnostics are open-file-only, automatic import completions are disabled, and
common generated/data folders are excluded by default. Servers supporting an
indexing control also receive an indexing-disabled hint. Server/project
configuration can override exclusions. `.venv` discovery is left to the server; no
remote interpreter or virtualenv is executed or downloaded by Naui. Remote
environment parity is not promised.
Python metadata probes use a private wrapper around the local interpreter with
isolated mode (`-I`), so remote `sitecustomize.py` and `PYTHONPATH` cannot run
during interpreter discovery. The language server also starts in that private
local runtime directory; workspace URIs still point at mounted sources.
Without a usable mount, analysis downloads only .py/.pyi
sources through RemoteFileBackend into a private local cache under
$XDG_CACHE_HOME/naui/code-* (default ~/.cache/naui/). Limits: 256 source files,
16 MiB, 128 directories, depth 8, and a 60-second scan budget (an in-flight SSH
operation has its own bounded timeout). Partial indexing is reported. Symlink
entries and environment/build directories are skipped. Remote project configs,
virtualenvs and executables are not copied or run.

Pyright runs locally; no remote language server, package, daemon or persistent
configuration is installed. Analysis reflects local Python/typeshed/dependencies
and source access, rather than the remote Python environment. Definitions
in remote project sources map back to their original device; results outside
that device's mirror report a limitation, rather than treating local library
paths as remote files. Use Reindex after external remote source changes. Changed
remote files detected during rename require reindexing and retrying.

Language-server traffic uses bounded JSON-RPC over local stdio, not a network
listener. Unsolicited applyEdit/executeCommand requests are rejected. Hover
content is shown as plain text; server/file contents never become commands.
Document changes are debounced, stale diagnostics/results ignored, and closed
tab versions cannot overwrite a reopened tab. At most 8 project servers run per
application. Shutdown cancels requests, reaps servers/helpers, releases UI
callbacks on the main thread and removes mirrors after workers finish.
Language requests/indexing have a separate worker pool so a slow server does
not occupy the file-save workers. Protocol messages are capped at 16 MiB and
queued notifications at 4 MiB.

For an existing nonstandard installation, set a local argv command (no shell
evaluation); include the server's stdio flag:

```bash
NAUI_PYRIGHT_COMMAND='pyright-langserver --stdio' naui
```

Protocol: [Language Server Protocol 3.17](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/).
Server settings: [Pyright documentation](https://github.com/microsoft/pyright/blob/main/docs/settings.md).

## SSH and remote media

### Remote filesystem selection

SSH device **Properties → Remote Filesystem** offers **Automatic** (default),
**SSHFS**, and **Compatibility / SSH**. Both Automatic and SSHFS fall back safely
if detection, authentication, mounting or mounted reads fail. Properties shows
the current backend/reason. This release uses a hybrid backend: SSHFS supplies
read-only mounted roots, while text read/save revisions, rename/delete/create,
and transfer staging remain on the tested SSH transport. Directory listings use
mounted names plus one bounded remote metadata query to preserve revision checks.
The fallback uses OpenSSH and existing remote Bash/GNU coreutils; it does not
require a separately installed Naui or SFTP helper.

Only pinned roots mount, lazily, without recursive enumeration. Internal mounts
are private under `$XDG_RUNTIME_DIR/naui/mounts/<device-id>-<session>/<root-hash>/`;
if no valid runtime directory exists, a private per-user temporary runtime is
used. No full project mirror is created by SSHFS. Logical remote paths remain
in documents, tabs, workspace settings and navigation. Mounted roots are read-only,
noexec/nosuid/nodev and resolve symlinks on the server instead of exposing local
absolute symlinks. Git and runner commands continue executing on the remote host.
Transfers still stream through TransferManager, including remote→remote copies
with bounded buffers. Remote watches still use temporary remote inotify hints
and controlled polling, since FUSE notifications do not reliably describe
another user's changes.

A mount belongs to the device, independently of terminal tabs. When the final
terminal exits, an already-mounted device can retain filesystem access through
an independent transport; use **Disconnect** to release it. Its OpenSSH connection
does not reuse a terminal-owned control socket and may request authentication
separately. Authentication stays in core, with no passwords in mount commands.
Disconnect/shutdown stop owned readers and normally unmount via the FUSE utility.
Busy/stale mounts and their directories are retained if normal unmount fails;
Naui never recursively deletes mount directories or unmounts unknown older/user
mounts. Reconnect retries mounting. Unknown stale runtime directories require
owner inspection. SSHFS/macOS mounting is currently unsupported and untested.

```bash
ssh user@server
cd /tmp
nano naui-remote-test.txt
```

OpenSSH handles authentication, keys, ssh-agent, SSH config/aliases, ProxyJump
and known_hosts. Temporary Bash functions are injected only for that session;
remote startup files and /etc are unchanged. A temporary **local** ControlMaster
socket reuses the authenticated connection for files, including password-auth
sessions normally without an additional authentication prompt. Noninteractive SSH forms pass through
normally, without claiming editor/tree integration.

File commands originate locally and use shell-quoted positional arguments.
Remote editor tabs show host identity. Saves stage complete files and check
revisions before replacement. Disconnects preserve unsaved contents. Elevated
`sudo nano` opens the normal terminal editor locally and over SSH. Sudo handles authentication in the terminal; elevated graphical editing remains unsupported.

Remote media uses backend probe/copy_to operations and private 0700 per-app
cache directories under `$XDG_CACHE_HOME/naui/` (default `~/.cache/naui/`), with
0600 files. SSHFS media uses verified regular mounted files directly, without
downloading an entire video first; source paths are not deletable cache entries.
Compatibility downloads are capped locally at 256 MiB per file and 512 MiB total
per application (including reservations for concurrent downloads); close a
preview to release its space. Timeouts allow up to 180s
when a graphical authentication response is needed.
Players clear their files before tab-close deletion; application shutdown joins
workers before cleaning the entire private cache. Completed cached previews
remain available after SSH disconnect, with a disconnected tab marker. Forced
SIGKILL/crashes can leave private cache directories because cleanup cannot run.
New marked preview/source caches from dead processes are cleaned conservatively
on the next cache use. Live sessions, unmarked older caches and symlinks are left
alone. Inspection is bounded; stale unknown folders may need owner cleanup.

## Architecture and security

```text
VTE widget <-> VTE-owned PTY <-> raw slave <-> bounded OSC relay
                                               |
                                      shell PTY <-> real shell/OpenSSH
```

The shell has a real controlling terminal/job control; VTE renders it normally.
Bash prompt hooks report cwd/busy/readline readiness via private OSC JSON/base64,
preserving existing hooks. Tree navigation requires a fresh signed readiness
acknowledgment before sending quoted cd, so it does not feed commands into a
foreground program/password prompt. zsh has analogous temporary hooks.

OSC is untrusted and validated, with a 16 KiB bound, terminal token and local-only
HMAC signing key. OpenSSH's environment strips the key even with broad SendEnv.
Remote events are confined to registered SSH sessions. Ordinary output/file
contents/media never become commands. Scripts open as text; media is read-only
and never launches external apps. No SSH/sudo passwords are persisted; no remote
Naui packages, daemons, persistent config, HTTP/TCP server or CDN is required.

ViewerRegistry uses supported binary signatures, MIME hints, extensions and
UTF-8 fallback. GTK factories create TextEditorViewer (the existing editor),
ImageViewer, AudioViewer, VideoViewer or an unsupported tab. Registry, geometry/
time helpers, cache, file backends, protocol and SSH modules are GTK-independent.
Images use native GTK texture snapshots; media uses Gtk.MediaFile/Gtk.Video.
Workers hold plain data/backends, never native UI objects.

Shutdown remains idempotent: stop events/sources, cancel transfers/jobs,
disconnect callbacks, stop players, terminate/reap PTY helpers, let VTE release
its own PTY, join workers and clean resources before GTK's normal close. Native objects are
released on the main thread; crashes/stderr are not suppressed.

## Development and verification

See [Contributing](../CONTRIBUTING.md) and the public GitHub Actions workflow.
Run unit tests and native GUI smoke tests before publishing; local test results
do not establish behavior on every distribution or SSH host.

## Known limitations

* macOS planned/unvalidated. Linux /proc cleanup, renameat2, remote GNU coreutils,
  native dependencies and desktop conventions need adapters/testing. No .app.
* Media depends on installed codecs; GIF uses its first frame. Images are limited
  to 128 MiB/32 megapixels. Main-thread image decoding can briefly delay input.
  View rotation never changes the original.
* Compatibility remote media downloads fully before playback; SSHFS accesses
  mounted data on demand. No media editing/encoding.
  Unsupported playlists are not played.
* Text is regular UTF-8 files up to 8 MiB; at most 64 file tabs. Atomic replacement
  preserves ordinary permissions/ownership when possible, not inode identity,
  hard links, ACLs or xattrs. Optimistic revision checks have a small rename race.
* Failed saves preserve edits. Save-copy requires a new file. No recovery autosaves
  or graphical elevated saves. Ordinary nano flags/unnamed buffers are unsupported.
* Remote Bash/Linux is the supported integration target. zsh hooks are unvalidated
  here; other local shells run without nano/prompt integration.
* SSH terminal sessions own separate OpenSSH masters rather than sharing a single
  background master. A filesystem operation using a closing terminal's transport
  may fail safely; subsequent operations use a surviving session. For custom
  -i/-F/-o settings, configure an SSH alias; live options are not persisted.
* naui ., naui ssh HOST and naui HOST:/file remain future CLI extensions.

## Project metadata

MIT: [LICENSE](../LICENSE). [Changes](../CHANGELOG.md). [Contributing](../CONTRIBUTING.md).
Project: https://github.com/Est0Han/naui.

## Terminal tabs and presentation

Closing a terminal tab quiesces only that session and defers native page disposal
until its button/PTTY callback has returned. The session moves through Running,
Closing and Closed; duplicate close requests and late events are ignored. Owned
children are reaped and VTE is detached before page removal. Foreground programs
require confirmation. Local shell exit/EOF keeps the existing last-terminal
Exit Naui / Start New Terminal decision; remote exit never exits Naui.

The Devices section has an SSH action menu: Open Terminal, New Terminal,
Reconnect, Disconnect, Remove from Workspace and Properties. Disconnect ends
Naui-owned sessions, not the remote computer. Confirmed workspace removal cancels
transfers and releases watches while retaining unsaved documents and their
original backend. Save a local copy before discarding a removed device's edits.
No remote files or configuration are deleted.

Drag the native divider to resize the left sidebar. Transfers has its own activity panel.
Ctrl+B hides/restores the sidebar. View → Reset Layout restores usable sizes and
section expansion. Ctrl++ (also Ctrl+=), Ctrl+- and Ctrl+0 change/reset the active
terminal or text/diff font. There is one terminal font preference and one editor
font preference (6–32 pt), applied across their views. Images/media use their
existing viewer controls. Sidebar focus uses its independent scale preference;
these bindings can be changed in Keybindings settings.

Safe layout/font values live in `$XDG_CONFIG_HOME/naui/ui.json`, alongside the
workspace file. Window sizes are clamped to monitor bounds at startup; window
positions are not stored. Invalid existing preferences are preserved until an
explicit Reset Layout. Sidebar width/visibility/activity/scale/density, source-control expansion,
window dimensions and font preferences are remembered.

Desktop regression (actual rendered VTE tabs, 20 closes):

```bash
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/terminal_tabs_smoke.py
# Optional authorized SSH target exercises two terminals on the same device:
PYTHONPATH=src python3 tests/terminal_tabs_smoke.py --ssh-host YOUR_SSH_ALIAS
# The launcher is a Python script; tell gdb which interpreter to run:
gdb --args /usr/bin/python3 ./naui
# Automated native tab reproduction under gdb:
gdb --args /usr/bin/python3 tests/terminal_tabs_smoke.py
```

For the last command, set `PYTHONPATH=src` before invoking gdb. Inside gdb use
`run`, then `bt full` and `thread apply all bt full` if a crash occurs. The smoke
script returns 77 if no display is available, rather than claiming acceptance.
Native SIGSEGV absence must be verified on a desktop; headless widget mocks and
real PTY tests alone do not establish it.

Editor and UI shortcuts can be customized in **Settings → Keybindings**, including plugin commands. See [Keybindings](../docs/keybindings.md).

## Everyday development tools

Use **View → Command Palette** (`Ctrl+Shift+P`) to search available actions and
see their current shortcuts. Commands follow the active editor, terminal or
panel. Existing shortcuts remain configurable in Settings → Keybindings.

| Action | Default shortcut / location |
| --- | --- |
| Recent tabs; hold Ctrl and repeat Tab to cycle | Ctrl+Tab / Ctrl+Shift+Tab |
| Reopen the last closed file | Ctrl+Alt+T |
| New terminal (unchanged) | Ctrl+Shift+T |
| Pin / unpin a tab | View or Command Palette |
| Run / save a named project task | View or Command Palette |
| Preview workspace replacement | Ctrl+Shift+H or Search → Replace |
| Code completion | Ctrl+Space |
| Review a quick fix | Ctrl+. |
| Review document formatting | Ctrl+Shift+I |

Tabs can be reordered and dragged between the two split groups. Pinned tabs
show a pin and hide the close button. Each group shows its active device.

Editor files, cursor positions, pinned tabs and split orientation are restored
on startup. Unsaved drafts are checkpointed every five seconds and on normal
exit to a private `session.json` beside the workspace settings (32 MiB combined
text, 64 files). Closing the application offers **Keep Drafts and Close**; closing
an individual dirty file still asks before discarding it. Restored drafts retain
the original revision, so saving still checks for external changes. Remote file
snapshots reopen offline; terminals/processes and SSH connections are not
restarted automatically. Unavailable-device drafts remain in recovery storage.

Project tasks store a name, explicit device, directory and argument list in
`tasks.json`. They run only after you choose a task and review those details.
Execution uses the existing acknowledged terminal command route. Save named
Build, Test, Run or Format tasks appropriate to your project; quoted arguments
are preserved. Pipelines require an explicit shell command such as `bash -lc`.

Workspace replacement requires a completed content search without truncation
or warnings and at most 64 matched files. Save affected dirty files first.
Review every affected file in a dark diff before applying. Changes go into
undoable editor buffers; Save remains explicit and revision checked. Regex
replacement uses Python capture-group syntax and runs in a separate process
with a timeout. Text files remain limited to 8 MiB; previews total at most 32 MiB.

Completion, formatting and quick fixes use the active language provider.
Availability depends on the server: bundled Python support uses local Pyright,
which does not provide document formatting; use a Format task or a language
plugin that supplies formatting. Quick fixes accept reviewable text edits only;
server commands and snippet completions are not executed. Diagnostics and
clickable terminal paths keep their existing device identity; compiler
`file.cs(line,column)` links are also recognized.

Tracked text diffs offer **Stage Change** / **Unstage Change** for the block
selected with Previous / Next Change. The diff must still match the reviewed
version. Reopen it after staging. New/deleted/renamed files, binary changes and
mode changes use the existing whole-file staging controls.

Additional checks:

```bash
PYTHONPATH=src python3 tests/development_smoke.py
PYTHONPATH=src python3 tests/code_assist_smoke.py  # requires local Pyright
```

### Explorer resizing and named layouts

Drag the thin handle below a device's folder list to set its height. Each device
remembers its own height (40–1200 pixels). The focused handle also accepts Up/Down
in 20-pixel steps; double-click or Home restores automatic content-sized height.
The Explorer overflow's global tree-height control resets those manual sizes.
Sidebar width and editor split proportions are also saved automatically.

**View → Layout Presets** saves the current panel sizes, sidebar visibility and
activity, device-card expansion, split arrangement/proportion, fonts, and visual
editor preferences. Click a saved name to apply it; use Save Current Layout,
Update Saved Preset, Rename Preset, or Delete Preset to manage up to 32 layouts.
Presets are independent snapshots: resizing does not overwrite a named preset
until you choose Update. They never contain file contents or connection settings,
never reconnect devices, and never close tabs or discard edits. Applying a split
layout needs at least two open tabs. Presets live in `layouts.json` next to the
workspace settings; the most recent manual sizes remain in `ui.json`.

```bash
PYTHONPATH=src python3 tests/layout_presets_smoke.py
```

### Preview file tabs and personal plugins

Single-click a file in Explorer to open an italic preview tab. Git diffs, text
output, browser views, plugin management and settings tabs use the same preview
behavior. Opening another content tab replaces the unmodified preview in that
tab group. Double-click the file or
its tab to keep it open without pinning; editing also keeps it open. Pinning keeps
a tab open and shows a pin icon; unpinned tabs have no pin icon. Terminals and
restored documents are not replaced, and each split group has its own preview.
Views with active operations or dialogs stay open until explicitly closed.

Generated personal plugins include a `.gitignore` that excludes their contents
by default. Remove that rule if you deliberately want to version a plugin.
The checkout's root `plugins/` folder is also ignored; bundled plugins under
`src/naui/plugins/` and SDK examples remain version-controlled. Normal installed
plugins live outside this checkout in the user configuration directory.

Right-click a content tab for Save, Save a Copy, Keep Open, Pin/Unpin, side-by-side
or top/bottom split, Move to Other Group, Copy File Path and Close where applicable.
Actions target the clicked tab. Duplicate Tab as Snapshot opens a read-only copy
of current text (including unsaved editor content) or the current diff; it does
not create a second writable buffer for the same file. The source stays open.

The toolbar includes a compact right-panel toggle for AI and other plugin panels.
Hiding a panel preserves its input and running work. Streaming panel text follows
new output when already at the bottom; scrolling up keeps the reader's position.
Plugins can use labelled action buttons for sign-in, approval and task controls.

# Changelog

## Unreleased

* Add simultaneous device/repository Source Control groups, staged/working/conflict
  states, outgoing commit details and locally known incoming counts.
* Add repository-scoped stage/unstage, staged-only commits, explicit fetch/pull/push
  and confirmed discard. Untracked deletion uses revision-checked filesystem APIs
  and refuses directories; remote Git continues executing over SSH.
* Add reusable theme-aware side-by-side/inline diff tabs, logical line numbers,
  hunk commands and large/binary safeguards, shared with history and AI reviews.
* Add temporary-repository/bare-remote tests and desktop mixed-tab stress acceptance.

* Enhance GtkSourceView editing with device-owned document metadata, undoable
  multi-line comments/indentation, line moves/duplication/deletion, asynchronous
  find/replace and go-to-line. Add editor preferences and large-file safeguards.
* Add diagnostic gutter marks/tooltips and a permission-checked public line
  command API. Keep terminal EOF shortcuts separate from editor shortcuts.

* Add optional, lazy, device-owned read-only SSHFS roots with Automatic/SSHFS/SSH
  compatibility modes, private runtime paths, logical URI mapping and safe teardown.
* Use mounted files for media and local language tooling while retaining remote
  Git, execution, watches, revision-checked writes and TransferManager streams.
* Bound aggregate compatibility media cache to 512 MiB; retain the 256 MiB file cap.
* Sanitize infrastructure-specific fixture names and diagnostic-run identifiers;
  add a release hygiene audit and disposable install/backend regression checks.

* Add core-owned AI Agent Manager with dynamically detected Codex/Claude,
  configured custom argv/prompt delivery, optional model IDs and explicit task
  launch into existing independent PTYs. API adapters remain planned.
* Integrate provider settings, task controls, bounded snapshot/diff review and
  fresh repair tasks into Plugins; retain manual master/task prompts.
* Filter task environments, keep prompts runtime-only and validate with the
  current launched build/SDK. Never auto-enable generated code.
* Add plugin list/paths/enable/disable/uninstall recovery CLI, temporary safe
  mode and persisted failed/interrupted startup guards; retain user settings.
* Add mocked agent/real-PTY lifecycle tests and native desktop acceptance.

* Add a first-class main-area Plugin Manager with discovery, permission/trust
  review, disabled Folder/ZIP install, validation, enable/disable/reload,
  safe uninstall, folders/terminals and bounded session logs.
* Add public naui_plugin_api SDK v1, TOML manifests (legacy JSON retained),
  permission aliases, controlled settings and device-aware operation facades.
* Add AI scaffolds and create/modify/error prompts with actual SDK paths and
  the assigned-plugin-only / PLUGIN_API_CAPABILITY_MISSING conventions.
* Add headless plugin CLI validation with bounded import/test subprocesses,
  AST boundary checks, content-fingerprinted saved grants and fresh reloads.
* Ship four small SDK examples and developer docs with user installation.

* Constrain the left sidebar to vertical scrolling, ellipsize long labels and
  cap tree indentation. Inline fields scroll internally; compact icon actions
  and workspace/device overflow menus retain the existing filesystem actions.
* Persist 180–600 px sidebar sizing, section expansion and selected device.
* Keep Source Control History/Refresh actions visible even when collapsed;
  display detection/loading/disconnection errors and select the correct root.
* Add bounded per-device Git history main tabs, branch/tag filters, simple
  parent lanes, full commit details and changed-file diffs through public APIs.
* Add real local Git merge/rename/history fixtures, simulated remote routing,
  pagination/permission/lifecycle checks and rendered-sidebar desktop acceptance.

* Quiesce terminal callbacks before deferred main-loop disposal; remove exact
  tab mappings before GTK page switching, with idempotent per-terminal lifecycle.
* Allow independent SSH terminal transports per device. Closing one preserves
  surviving sessions, backend identity and editors; late session exits are ignored.
* Add explicit sidebar SSH action menus and confirmed workspace removal that
  keeps unsaved documents and never modifies remote files.
* Persist native split-pane layout, bounded terminal/editor fonts and window size;
  add Ctrl+B sidebar toggle, View reset and contextual Ctrl++/-/0 zoom.
* Add headless tab-order/SSH/layout tests and a 20-close native desktop harness.
  Desktop/gdb validation requires an accessible display and ptrace permission.

* Retain the resizable left sidebar with Devices/Files/Source Control sections
  and safe XDG width settings. Update affected directories without resetting trees.
* Add core shared native local watches, adaptive bounded SSH snapshots and
  optional transient inotify hints with EOF cleanup, disconnect and shutdown guards.
* Preserve clean/dirty text on external edits, offer comparison/reload/explicit
  Keep My Version, track reliable file renames and retain deleted file buffers.
* Add public watch/command/terminal/view APIs and bundled base-runner/base-git.
  Runs use device-owned real PTYs, readiness acknowledgement and safe argv quoting.
* Add per-device Git detection, porcelain v2 status, tree decorations, debounced
  reactive Source Control groups and dark read-only staged/unstaged diff tabs.

* Add bundled/default-enabled plugin registration and scoped public APIs; move
  filepath/LSP implementations into their bundled owners, separate Pyright and
  keep compatibility imports. Add explicit trusted user-plugin loading/example.
* Add core bounded binary transfers in all local/SSH directions, including
  remote-to-remote streaming through Naui, progressive folder copies, conflicts,
  backend Keep Both, safe staging, pause/resume/cancel/retry and native tree DND.
* Add local Unix askpass brokerage with masked, device-scoped session-only memory;
  credentials and SSH remain core-owned, outside the public plugin API.
* Add explicit device Disconnect, local terminal creation/tab-close warnings,
  last-local-shell exit confirmation, remote exit isolation and cleanup coverage.

* Add a bounded local stdio LSP client and Pyright sessions per device/project.
* Add F12/Ctrl+Click definition, Shift+F12 references, hover, diagnostics,
  reviewed undoable symbol rename and device-owned code navigation history.
* Add read-only editor/terminal file-path links and bounded private remote Python
  source mirrors; remote servers require no Naui/Pyright installation.
* Protect stale/closed document versions, reconnect-sensitive rename previews,
  source cache ownership, and language-server shutdown/reaping.
* Add simultaneous local/SSH workspace panels, pinned roots, connection icons,
  alias suggestions and safe XDG workspace persistence.
* Separate PTY event/navigation state per terminal and keep document device and
  backend identities through switching and SSH reconnects.
* Add inline file/folder creation and rename, selection after completion, safe
  confirmed deletion and reference-only Remove from Workspace.
* Preserve per-device/root history and expansion state; restore remote cwd on
  reconnect through the existing shell readiness protocol.
* Extend native tree, save ownership, reconnect and multi-terminal shutdown
  tests; add a desktop workspace test with disposable SSH simulations.

## 0.2.0

* Add a MIME/signature-aware viewer registry while retaining the dark text editor.
* Add image fit/zoom/dimensions/view rotation and GTK audio/video playback controls.
* Add bounded read-only remote media downloads into private temporary local caches.
* Add About/project metadata and a single authoritative version.
* Add manifest-based Linux user install/uninstall, desktop entry and placeholder icon.
* Expand preview, installer, metadata and shutdown regression tests; generated media
  desktop checks; planned macOS packaging and toolkit evaluation.

## 0.1.0

* Native GTK4/VTE terminal, GtkSourceView editor, local/SSH nano interception.
* Synchronized lazy file tree/path bar and local/remote filesystem backends.
* Revision-checked saves, explicit destructive actions and dirty-close protection.
* Idempotent shutdown, tracked callbacks and owned PTY/OpenSSH reaping. The
  reported native SIGSEGV still requires desktop/gdb confirmation.

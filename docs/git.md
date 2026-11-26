# Source Control

Source Control lives in the left sidebar. Each repository has its own device,
logical root, branch and status. Multiple local and SSH repositories can remain
visible together. Expand a repository to inspect its groups; long names are
ellipsized and show their full logical path in a tooltip.

- **Changes** compares the Git index with the working tree. It includes untracked
  files (`?`), which preview as an empty file compared with the new file.
- **Staged Changes** compares HEAD with the index. Only staged content goes into
  the next commit. A file changed again after staging appears in both groups,
  with different comparisons.
- **Conflicts** (`!`) opens the normal editor for conflict-marker resolution.
  Stage the resolved file afterward; there is no automatic merge editor.
- **Outgoing Commits** are committed changes not yet pushed to the configured
  upstream. Click a commit for its message, parents, files and historical diffs.
  A clean working tree can still have outgoing commits.
- **Incoming** is the locally known upstream's ahead/behind information. Naui
  does not automatically fetch. Use **Fetch** to update that information.

`M`, `A`, `D`, `R`, `C` mean modified, added, deleted, renamed and copied. Git
identifies renames/copies where its status output supports them. Ignored files
remain hidden in normal status; they are not treated as pending changes. Detached
HEAD shows the commit ID. Missing Git, disconnected devices and command errors
have visible states rather than removing the Source Control section.

Click a changed file for a read-only main-area diff. **Side by Side** aligns old
and new hunk lines; **Inline** shows both versions in one pane. Line numbers are
original/new file numbers, rather than patch row numbers. Previous/Next Change
navigate hunks (`Shift+F7` / `F7` by default, configurable in Keybindings).
**Open File** opens the working file in the normal editor. Binary changes show a
message instead of decoded binary content. Files with no working version remain
viewable through their Git patch. Pure renames show rename metadata.

Diffs over 1 MiB or 12,000 patch lines require **Open Anyway**. Rendering is
capped at 8 MiB / 60,000 lines; larger command output is bounded by the core
command service. Untracked files over 8 MiB are not downloaded for diff preview.
Use the normal editor, media viewer or terminal for files beyond these limits.

The file's right-click or overflow menu offers **Stage**, **Unstage**, **Open
File**, **Copy Path**, and **Reveal in File Tree**. Unstaging preserves working
files, including on a branch without its first commit. Repository actions offer
**Stage All** and **Unstage All**, scoped to that repository. Commit messages
appear below staged changes. Messages must be non-empty; nothing is silently
staged, and a failed commit preserves the draft and displays the Git error.

**Discard** always asks for confirmation. Tracked-file discard restores the index
version and preserves staged content. Untracked-file discard explicitly deletes
one file using Naui's filesystem backend, with revision checks; directories are
refused. Tracked-file restore also refuses an existing directory at the file path. Conflicted files are not automatically discarded. A file's Git state is
checked again before mutations; an external change after that check can still
race a Git operation. Unsaved editor buffers are governed by the existing
external-change/conflict workflow.

**Push**, **Pull**, and **Fetch** are explicit repository actions. Push/Pull
require configured upstream tracking. Pull uses `--ff-only`: it never silently
rebases, resets or discards files. Git errors and hooks are surfaced; long-running
hooks/network operations remain subject to the core command timeout. Configure
Git identity, remotes, credential helpers and upstreams using normal Git tools.
Naui does not collect or log Git/SSH credentials. Git 2.23+ is needed for tracked
file discard (`git restore`).

For SSH repositories, status, diffs, staging, commits and network operations run
**on the SSH device** through Naui's command API. SSHFS is used for file access,
not local Git execution. Titles, tree entries and clipboard paths retain logical
device paths. Repository identity always includes both device ID and root.

Status refreshes share FileWatchService and debounce filesystem events by 500 ms.
Saving a file and explicit Git actions refresh status without polling on every
keystroke. History refreshes on HEAD/ref changes or explicit refresh. Nested
remote ref-directory changes follow the existing watch coverage; Refresh/Fetch
is available when locally known upstream state is stale.

The command registry exposes `git.open_source_control`, `git.open_history`,
`git.refresh`, `git.open_diff`, `git.stage`, `git.unstage`, `git.next_change` and
`git.previous_change`. File-level command bindings use the active working/index
diff's repository and file; they never guess from a different selected device.

Source Control has a dedicated Activity Rail panel (Ctrl+Shift+G by default),
rather than appearing below Explorer. Its badge counts changed files once;
repository/status groups fill the panel and retain expansion when switching
activities. Heading Refresh is visible and History is in its overflow menu.
Diff/history tabs remain open when switching back to Explorer.

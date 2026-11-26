# Programming editor

Naui retains **GtkSourceView 5** inside GTK 4. It supplies the native multiline
selection, undo stack, language grammars, search engine, bracket highlighting,
line numbers and basic automatic indentation. No extra editor engine or web
runtime is installed.

## Commands

Shortcuts apply when the text editor itself has focus. They do not intercept
terminal EOF, signals or terminal input. In search fields, native text-entry
shortcuts remain available.

| Shortcut | Action |
| --- | --- |
| Ctrl+/ | Toggle comments for current/selected lines |
| Tab / Shift+Tab | Indent/outdent selected lines; native tab stops at one caret |
| Alt+Up / Alt+Down | Move current/selected lines |
| Ctrl+Shift+D | Duplicate current/selected lines |
| Ctrl+Shift+K | Delete current/selected lines |
| Ctrl+D | Select next occurrence; **replaces**, rather than adds to, selection |
| Ctrl+F / Ctrl+H | Find / Replace |
| Ctrl+G | Go to line, numbered from 1 |
| Ctrl+Z / Ctrl+Shift+Z | Undo / Redo |
| Ctrl+S | Save through the originating backend, with revision checks |
| Ctrl++ / Ctrl+- / Ctrl+0 | Editor font zoom / reset |
| F12 / Shift+F12 / Ctrl+K / F2 | Definition / references / hover / rename |

An endpoint at the next line's column zero excludes that next line from line
commands. Comments and indentation keep multiline selections selected (expanded
to complete lines), including reverse selections. Moving lines retains the
original selection columns. Each line command is one undo action.

Comment syntax comes from the language grammar's metadata, including `#`, `//`,
`;` and `--`. Languages offering only block comments use a wrapper on each
selected nonblank line. Blank lines are preserved. Mixed commented/uncommented
selections are commented as a whole; repeating the command restores them.
Strict JSON and unknown languages have no Toggle Comment action. Grammars are
selected from the filename, with a shebang hint for extensionless scripts.

Find/Replace provides forward/backward navigation, wrapping, match count,
case sensitivity, whole-word matching and the native regular-expression engine.
Regex replacements use backreferences such as `\1`; errors are shown in the
search row. Search navigation uses cancellable asynchronous GtkSourceView calls.
Replace changes the selected match and advances; Replace All is one undo action.

The native context menu keeps Undo/Redo/Cut/Copy/Paste and adds line commands,
search, go-to-line, definition/references, Copy Path and Reveal in File Tree.
Reveal changes the explorer presentation without sending `cd` to a terminal.
LSP diagnostics have underline, gutter icons and hover tooltips; diagnostics
are cleared immediately when their buffer version becomes stale.

## Preferences and large files

**View → Editor Settings** configures font size, tab/indent widths, spaces/tabs,
automatic indentation, wrapping, line numbers, current-line highlight,
whitespace display and save cleanup. Defaults: dark style, monospace, four
spaces, no wrapping, no whitespace markers. Preferences use the existing user
XDG settings. Reset Layout preserves editing preferences.

Trailing-whitespace trimming and final-newline insertion are **off by default**.
Opening a file does not rewrite it. Opt-in save transformations are undoable and
preserve existing LF/CRLF endings. UTF-8 decoding errors remain explicit; other
encodings are not silently converted. Automatic indentation uses the native
component; Naui does not implement a language parser for Python block indentation.

Large-file mode starts at 1,048,576 characters, 20,000 newlines or a
100,000-character line. It disables syntax/bracket highlighting, LSP, regex,
Replace All and automatic path highlighting. Ordinary editing and asynchronous
literal search remain available. Explicit Ctrl+Click paths still resolve on the
clicked line, limited to 16,384 characters. Mode remains active until reload.
The existing filesystem backend's **8 MiB text-editing limit** remains in place.

## Ownership and architecture

`EditorDocument` in `editor_model.py` owns logical device/path/backend identity,
revision, encoding, language, external-change state, large-file policy and LSP
version. Its text buffer uses a small adapter protocol without GTK imports.
`editor_commands.py` produces pure selection-aware edit plans. `EditorController`
in `editor_view.py` applies them to GtkSourceView and owns view actions, search,
dialogs and cancellable callbacks. `EditorPane` remains the tab and save/watch
host. Existing LSP, plugins and viewer APIs retain their document interface.

SSHFS and compatibility documents share these same commands. Saves still use
the document backend, never the currently selected tree or an internal mount
path. External/dirty conflict snapshots are preserved. Git observes saves through
the existing filesystem/workspace notifications; execution remains an explicit
runner action on the document's device.

Plugins can call permission-checked `api.editor.invoke(command)` for documented
line commands. This edits the active buffer only; it cannot execute scripts,
access native widgets or save automatically. See the Plugin SDK API reference.

## Component limits and future migration

The current public GtkSourceView API has one insertion mark and one selection
bound. It offers no robust multiple-selection/multiple-cursor editing or code
folding API. Naui deliberately does not fake either with extra marks or hidden
text: typing, IME, undo, diagnostics and lifecycle would become unreliable.

Retain GtkSourceView for this release. If multiple cursors and folding become
mandatory, evaluate an editor-only replacement. Scintilla/QScintilla needs a
GTK/Qt integration strategy; CodeMirror or Monaco would need a bundled offline
web renderer and a carefully constrained bridge. Migration cost is substantial
for text-delta synchronization, selection, undo, IME/accessibility and LSP
position translation, but should not require replacing terminal, SSH, filesystem
or device services. The document and command adapters isolate that future work.

## Validation

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/editor_smoke.py
```

The desktop smoke generates temporary Python files, checks line commands,
native search/replace, undo/redo, go-to-line and save, then removes twenty editor
tabs with pending search/GC and closes a second terminal while retaining one.
It exits 77 when no graphical display is available. Headless tests exercise real
GtkSource buffers and simulated remote saves; they do not claim live SSHFS or
rendered native lifecycle acceptance.

Shortcut customization is available in Settings → Keybindings. See [keybindings](keybindings.md) for contexts and import/export.

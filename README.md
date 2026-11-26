# NaUi

A Linux desktop workspace combining a real terminal, a code editor, local and SSH
file browsing, Git tools, and file previews. Built with Python, GTK4, VTE and
GtkSourceView.

**Linux desktop required.** Windows and macOS are not currently supported.
NaUi is early-stage software; see the limitations below before using it on
important projects.

## Install on Ubuntu / Debian

You need a desktop session and a distribution that provides GTK4, GtkSourceView 5
and **VTE GTK4 (`gir1.2-vte-3.91`)**. The project CI targets Ubuntu 24.04.

1. Install Git and Python if needed:

   ```bash
   sudo apt update
   sudo apt install git python3
   ```

2. Download NaUi:

   ```bash
   git clone https://github.com/Est0Han/naui.git
   cd naui
   ```

3. Install dependencies and NaUi:

   ```bash
   ./install.sh --install-deps --python /usr/bin/python3
   ```

   The installer uses your normal sudo prompt for system dependencies. NaUi
   itself installs in your home directory; do **not** run the entire installer
   with sudo.

4. Launch **Naui** from your application menu, or run:

   ```bash
   ~/.local/bin/naui
   ```

Once `~/.local/bin` is on your PATH, you can simply run `naui`.

### Fedora

```bash
sudo dnf install git python3
git clone https://github.com/Est0Han/naui.git
cd naui
./install.sh --install-deps --python /usr/bin/python3
~/.local/bin/naui
```

The installer checks dependencies before installing. For other distributions,
install Python 3.10+, PyGObject, GTK4, VTE GTK4, GtkSourceView 5, GdkPixbuf and
OpenSSH using your package manager, then run:

```bash
./install.sh --python /usr/bin/python3
```

See the [full guide](docs/user-guide.md#linux-installation) for manual package
lists and optional codecs. `pip install` alone does not install the required
GTK libraries and introspection bindings.

## First steps

- Use the terminal as a normal shell. Inside NaUi, `nano file.py` opens its
  graphical editor; `/usr/bin/nano` runs the original terminal editor.
- Browse local folders or connect over SSH. Remote machines use their existing
  OpenSSH, Bash and GNU coreutils; NaUi does not need to be installed there.
- Click a file for a temporary preview tab. Double-click the tab to keep it open.
- Right-click a tab for available save, close and split-view actions.
- Use Source Control to review changes and stage or unstage files.
- Install your own plugins through the Plugins page. Personal plugins and their
  tests are separate from the source tree and are not included in this repository.

## Features

- Real PTY terminal with interactive applications and multiple terminal tabs.
- Code editing with syntax highlighting, search, undo, save and conflict checks.
- Local/SSH explorer, file transfers, resizable panels and saved layouts.
- Side-by-side and stacked tab groups, preview tabs and pinned tabs.
- Workspace search and Git changes, staging, diffs and history.
- Optional Python code navigation through Pyright.
- Image, audio and video previews using installed system codecs.
- EXE/DLL inspection, plus optional Ghidra C-like decompilation previews.
- Optional RPF archive browsing and extraction through OpenRAGE.
- Plugin API and example plugins; no personal AI chat plugin is bundled.

## Optional tools

The basic terminal/editor works without these tools:

| Tool | Purpose |
| --- | --- |
| SSHFS (`sshfs` / `fuse-sshfs`) | Faster remote file access; the OpenSSH backend remains available without it. |
| Pyright | Python definitions, references and diagnostics. |
| [OpenRAGE](https://github.com/Two-Play/openrage) | RPF archive browsing and decoding. See [RPF support](docs/rpf-support.md). |
| [Ghidra](https://github.com/NationalSecurityAgency/ghidra) | Reconstructed C-like previews for native EXE/DLL files. See [executable inspection](docs/executables.md). |

These tools, Java, game files, encryption keys and extracted/decompiled files are
**not bundled**. Optional tools retain their own licenses.

## Update

From your cloned repository:

```bash
git pull --ff-only
./install.sh --python /usr/bin/python3
```

Close and reopen NaUi when your terminals and active tasks can safely stop.
The installer preserves unrelated files and refuses to overwrite modified
installation files.

## Uninstall

```bash
~/.local/share/naui/uninstall.sh
```

This removes files owned by the installer. User configuration and personal
plugins are preserved.

## Troubleshooting

- **`naui: command not found`:** use `~/.local/bin/naui`, or add
  `export PATH="$HOME/.local/bin:$PATH"` to your shell configuration.
- **Missing `gi`, `Gtk`, `Vte` or `GtkSource`:** rerun the dependency installer
  with `--python /usr/bin/python3`. GTK3 VTE (`2.91`) cannot replace GTK4 VTE
  (`3.91`). Virtual environments need `--system-site-packages`.
- **No display:** run NaUi in a graphical Linux session, not a headless shell.
- **A video or image will not open:** install the appropriate GStreamer/GdkPixbuf
  codec or loader for your distribution.
- **SSHFS unavailable:** use the built-in OpenSSH compatibility backend.
- **Permission errors:** do not run the whole app as root. Keep existing files
  backed up and use the terminal for operations requiring elevation.

## Limits and safety

EXE/DLL inspection never executes the selected binary. Decompiled output is
inferred pseudocode, not the exact original source, and is a bounded preview.
RPF archives remain read-only: editing an extracted file does not repack it.
Some archive formats, encrypted entries and compiled scripts cannot be decoded.

Plugins run trusted Python code in the application process; API permissions are
not a sandbox. Review plugins before enabling them. Real SSH authentication,
codecs and desktop integration can vary by machine.

## Documentation and development

- [Full user guide](docs/user-guide.md)
- [Plugin development](docs/plugin-development/API_REFERENCE.md)
- [Plugin testing](docs/plugin-development/TESTING.md)
- [Contributing and verification](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

Run the source checkout without installing with `./naui`. For release checks:

```bash
sh scripts/release-audit.sh --tests
```

The test suite uses temporary fixtures; it does not require proprietary game
files. GUI smoke tests require a desktop or Xvfb.

## License

NaUi is released under the [MIT license](LICENSE).

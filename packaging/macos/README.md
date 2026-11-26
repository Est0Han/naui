# macOS: planned, no tested bundle

The executable intentionally rejects macOS. No Naui.app is built/signed/tested,
and the Linux installer does not masquerade as one. Future delivery should work
from Finder/Spotlight and /Applications, with an optional Terminal CLI shim.

## Retain GTK/VTE now

VTE already handles interactive terminal rendering, replies, full-screen apps,
selection and resize. Media fits GTK's system MediaFile implementation. A Qt
migration also requires replacing the terminal renderer and validating OSC,
job control, sudo, readline and shutdown. That is larger than this extension.

GTK platform backends alone do not make this stack a tested macOS application.
GI/native dependency distribution and VTE suitability on Darwin need evaluation.
Cleanup uses Linux /proc session discovery, local safe rename uses renameat2,
and Linux desktop/icon conventions do not apply to Mac. Remote integration
assumes Linux Bash/GNU coreutils; servers can remain Linux with a Mac frontend.

Protocol, SSH, registry, geometry/time helpers, preview cache and most filesystem
code are GTK-independent. POSIX PTY setup is separate from its GLib relay. Add
Darwin process/rename and frontend event-loop adapters; retain tested session/file
logic. Mac cache defaults should use Library/Caches. Finder environment, shell
selection and SSH agent/config behavior need real tests.

## Qt frontend evaluation

PySide6/Qt6 offers Linux/macOS widgets, image APIs and
[QMediaPlayer/QAudioOutput/QVideoWidget](https://doc.qt.io/qtforpython-6/PySide6/QtMultimedia/QMediaPlayer.html).
It is a reasonable additional frontend once a real Mac and terminal acceptance
tests are available. Bundled offline xterm.js/QtWebEngine is possible, but adds
runtime size, assets and bridge lifetime complexity. It must retain a real PTY,
ship all JS/fonts locally and use no CDN/server. Do not migrate for nominal
portability or replace the shell with command emulation.

## Future packaging experiment

Prefer evaluating a directory-mode **PyInstaller bundle built on macOS** after
the frontend/PTY works there. Its app-bundle/PySide support is a smaller experiment
than introducing Briefcase's full project layout now. See
[PyInstaller macOS bundle documentation](https://pyinstaller.org/en/stable/usage.html#building-mac-os-x-app-bundles).
Briefcase remains an alternative if native dependency/distribution needs favor it.

The actual build must include native libraries/plugins/resources, a generated
.icns from the placeholder SVG, architecture-specific testing (Apple Silicon and
Intel if supported), Finder/Spotlight launch, system OpenSSH, optional CLI
integration and signing/notarization. GTK would also need typelibs/module discovery,
VTE and media plugins; Qt needs platform/multimedia plugins. Keep Mac packaging
separate from Linux. No speculative stub .app or spec is offered as a working build.

SSHFS backend detection currently deliberately selects compatibility on Darwin.
macFUSE/SSHFS availability, permissions, unmount behavior and mounted media/LSP
paths require real Mac tests before enabling this integration. Linux mounting
must not be advertised as macOS support.

# Contributing

Before a public commit, run `sh scripts/release-audit.sh --tests`. This checks
commit-eligible files for machine identities, non-example paths/addresses,
common credential patterns, binaries, crash artifacts and version consistency,
then runs unit tests and SDK example validation. It prints locations/categories,
not matching credentials. This is release hygiene, not a complete secret scanner.
Review screenshots manually; use only generic example identities. Do not commit
runtime configuration, caches, user plugins, keys or diagnostic logs.

Keep Linux functional and changes focused. Read README's architecture/security
sections before changing PTY, OSC, SSH or shutdown code. macOS is planned; label
platform results honestly and do not publish untested `.app` artifacts.

Use Python 3.10+ and distro GI packages; virtualenvs need
`--system-site-packages`. Run syntax checks and the regression suite from README.
On a desktop also run GUI/media/shutdown smoke tests. Include distro/library
versions and any SKIPs in a PR. Installer tests must use disposable prefixes.

Workers must capture only plain data/backends, not GTK/GObject wrappers. Keep UI
completion callbacks owned/cancelled on the main thread. Track GLib sources and
relevant signals. Stop codecs/processes before deleting resources or letting GTK
close widgets; maintain idempotent cleanup and late-event guards.

Register file types in viewer_registry.py rather than branching throughout the
GUI. Previews are read-only. Unknown data must not launch programs. Generate
fixtures in temporary directories; no downloaded media is needed. Maintain
response limits and quoted positional SSH arguments. No persistent remote
packages, startup edits or daemons.

Never commit keys, passwords, personal paths, local config, virtualenvs, core
dumps, cache or build output. Report security issues through the actual project's
configured private contact when available; no fabricated account/contact is used.
Do not put secrets in public issues. Project and placeholder icon use MIT.

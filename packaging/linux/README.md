# Linux packaging

./install.sh delegates to scripts/install.sh and the GTK-independent
naui.installation module. Resources are copied into a user prefix with a
shell-quoted Python launcher, a desktop-quoted absolute Exec entry based on
naui.desktop.in and a scalable SVG icon.

All conflicts are checked before writing. Managed files are staged and replaced
atomically; failures roll back; a digest ownership manifest is published last.
Uninstall preserves modified/unowned files and prunes only empty application
directories. Shared desktop/icon caches are refreshed rather than deleted.
Dependencies are distro-managed, with explicit optional sudo installation.

Future distro packages should use package-manager ownership instead of the user
installer. Depend on GTK4/VTE GTK4/GtkSourceView/GdkPixbuf/OpenSSH and recommend
distro codecs and SSHFS/FUSE. SSHFS is optional; missing tools/FUSE result in a
non-blocking compatibility notice, not an install failure. No system packages
are installed without the explicit --install-deps option, and SSHFS is never
added by that option. Do not bundle remote Naui components or authentication code.

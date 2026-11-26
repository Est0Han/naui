"""CLI parsing does not load GTK until after dependency and platform checks."""
import argparse
import os
import sys
from .metadata import version_string


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'plugin':
        from .plugin_cli import main as plugin_main
        return plugin_main(argv[1:])
    parser = argparse.ArgumentParser(prog="naui", description="Local terminal, graphical editor and media viewer")
    parser.add_argument("files", nargs="*", help="Local files to edit or preview beside the terminal")
    parser.add_argument("--plugin", action="append", default=[], help="Trusted local plugin directory; requires an in-app permission confirmation")
    parser.add_argument('--dev-plugins', action='store_true', help='Open the Plugin Manager for development')
    parser.add_argument('--safe-mode', action='store_true', help='Disable user plugins for this launch without changing saved grants')
    parser.add_argument("--version", action="version", version=version_string())
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("Naui currently supports Linux only")
    if any(os.path.isdir(path) for path in args.files):
        parser.error("Directory CLI arguments are reserved for a future project view; pass filenames")
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Vte", "3.91")
        gi.require_version("GtkSource", "5")
        gi.require_version("GdkPixbuf", "2.0")
        gi.require_version("Graphene", "1.0")
    except (ImportError, ValueError) as exc:
        print(f"Naui needs GTK 4, VTE GTK4, GtkSourceView 5 and GdkPixbuf Python bindings: {exc}", file=sys.stderr)
        print("Run ./install.sh --install-deps --python /usr/bin/python3, or see README.md for distro packages.", file=sys.stderr)
        return 1
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("Naui requires a Linux graphical desktop session (DISPLAY or WAYLAND_DISPLAY).", file=sys.stderr)
        return 1
    from .gui import NauiApplication
    from gi.repository import Gdk, Gtk
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print("Naui cannot access the graphical display from this session. Start it from your Linux desktop terminal.", file=sys.stderr)
        return 1
    app = NauiApplication(args.files, os.getcwd(), () if args.safe_mode else args.plugin)
    app.dev_plugins = args.dev_plugins
    app.safe_mode = args.safe_mode
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())

"""User Linux installation with collision checks and a bounded ownership manifest.

No GUI imports, system directories or shell evaluation. Uninstall only unlinks
manifest-listed regular files whose installed digest still matches; modified
files, user configuration, cache, and unrelated files/directories are preserved.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import __version__

MANIFEST = "share/naui/.naui-install.json"
EXTERNAL_FILES = {"bin/naui", "share/applications/naui.desktop",
                  "share/icons/hicolor/scalable/apps/naui.svg"}
APP_FILES = {"naui", "README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md", "uninstall.sh",
             "scripts/uninstall.sh", "scripts/package.py", "assets/icons/naui.svg",
             "docs/plugin-development/AGENT_INSTRUCTIONS.md", "docs/plugin-development/API_REFERENCE.md",
             "docs/plugin-development/SECURITY.md", "examples/plugins/inspect-selection/plugin.py",
             "examples/plugins/inspect-selection/plugin.json"}
APP_FILES.update({'docs/user-guide.md', 'docs/rpf-support.md', 'docs/executables.md', 'docs/plugin-development/UI_GUIDE.md', 'docs/plugin-development/MANIFEST.md',
                  'docs/editor.md', 'docs/keybindings.md', 'docs/git.md', 'docs/sidebar.md', 'docs/search.md',
                  'docs/plugin-development/TESTING.md',
                  'docs/plugin-development/AI_AGENTS.md',
                  'src/naui_plugin_api/__init__.py'})
for _example in ('hello-world', 'sidebar-panel', 'file-action', 'terminal-command'):
    APP_FILES.update('examples/plugins/' + _example + '/' + name
                     for name in ('plugin.toml', 'plugin.py', 'README.md', 'tests/test_plugin.py'))


@dataclass(frozen=True)
class InstallPaths:
    prefix: Path

    @classmethod
    def make(cls, prefix=None):
        path = Path(prefix).expanduser().resolve() if prefix else (Path.home() / ".local").resolve()
        if path == Path("/") or any(character in str(path) for character in "\n\r\0"):
            raise ValueError("Choose a user installation prefix without newline/NUL characters")
        return cls(path)

    @property
    def app(self):
        return self.prefix / "share/naui"

    @property
    def launcher(self):
        return self.prefix / "bin/naui"

    @property
    def manifest(self):
        return self.prefix / MANIFEST


def desktop_exec(path):
    path = str(path)
    if any(character in path for character in "\n\r\0="):
        raise ValueError("Desktop launcher path cannot contain newline, NUL or '='")
    # Two parsing layers: desktop string escapes, then Exec argument quoting.
    # https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html
    replacements = {"\\": "\\\\\\\\", '"': '\\\\"', "$": "\\\\$", "`": "\\\\`", "%": "%%"}
    # GIO checks the executable before expanding %% field codes; a percent in
    # the executable filename can otherwise fail despite correct Exec syntax.
    # /bin/sh is fixed, and the installed script path is a quoted DATA argument.
    return '/bin/sh "' + "".join(replacements.get(character, character) for character in path) + '" %F'


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _allowed(relative):
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or str(parts) != relative:
        return False
    if relative in EXTERNAL_FILES:
        return True
    if not relative.startswith("share/naui/"):
        return False
    name = relative[len("share/naui/"):]
    return name in APP_FILES or (name.startswith("src/naui/") and name.endswith(".py"))


def _safe_path(paths, relative):
    target = paths.prefix / relative
    # The resolved PREFIX may itself have been a symlink, but no installed
    # descendant may redirect writes/deletes to a different directory.
    for component in [target, *target.parents]:
        if component == paths.prefix:
            break
        if component.is_symlink():
            raise ValueError("Refusing an installation path containing a symlink: " + str(component))
    return target


def _read_manifest(paths):
    location = _safe_path(paths, MANIFEST)
    if not location.exists():
        return {}
    manifest = json.loads(location.read_text())
    if (not isinstance(manifest, dict) or manifest.get("application") != "Naui" or manifest.get("schema") != 1
            or manifest.get("prefix") != str(paths.prefix) or not isinstance(manifest.get("files"), dict)):
        raise ValueError("Invalid Naui installation manifest")
    files = manifest["files"]
    if len(files) > 2000 or any(not _allowed(name) or not isinstance(digest, str)
                              or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                              for name, digest in files.items()):
        raise ValueError("Invalid paths/digests in Naui installation manifest")
    return files


def installation_plan(source, paths, python):
    source = Path(source)
    result = {}
    for path in sorted((source / "src/naui").rglob("*.py")):
        if path.is_symlink():
            raise ValueError("Source package must not contain symlinked Python files")
        result["share/naui/" + path.relative_to(source).as_posix()] = (path.read_bytes(), 0o644)
    if "share/naui/src/naui/__main__.py" not in result:
        raise ValueError("Source tree is missing the Naui package")
    for name in APP_FILES:
        path = source / name
        result["share/naui/" + name] = (path.read_bytes(), 0o755 if name.endswith(".sh") or name == "naui" else 0o644)
    launcher = ("#!/bin/sh\n# Installed by Naui; do not write bytecode into the application resources.\n"
                "export PYTHONDONTWRITEBYTECODE=1\nexec " + shlex.quote(str(python)) + " -B "
                + shlex.quote(str(paths.app / "naui")) + ' "$@"\n')
    result["bin/naui"] = (launcher.encode(), 0o755)
    template = (source / "packaging/linux/naui.desktop.in").read_text()
    result["share/applications/naui.desktop"] = (template.replace("@EXEC@", desktop_exec(paths.launcher)).encode(), 0o644)
    result["share/icons/hicolor/scalable/apps/naui.svg"] = ((source / "assets/icons/naui.svg").read_bytes(), 0o644)
    return result


def install(source, paths, python=sys.executable):
    previous = _read_manifest(paths)
    if paths.app.exists() and not paths.manifest.exists():
        raise ValueError("Refusing to replace an unowned application directory: " + str(paths.app))
    plan = installation_plan(source, paths, python)
    # Preflight ALL conflicts before changing anything. Reinstall accepts only
    # files owned by the previous manifest, never a pre-existing unrelated nano.
    for relative in plan:
        target = _safe_path(paths, relative)
        if target.exists() and (not target.is_file() or relative not in previous
                                or _digest(target.read_bytes()) != previous[relative]):
            raise ValueError("Refusing to overwrite an unowned or modified file: " + str(target))
    paths.prefix.mkdir(parents=True, exist_ok=True)
    installed = dict(previous)
    # Stage complete content and back up each managed file. Commit per-file
    # atomically and roll back on failure; publish the new manifest LAST.
    with tempfile.TemporaryDirectory(prefix=".naui-install-", dir=paths.prefix) as temporary:
        stage = Path(temporary)
        committed = []
        try:
            for index, (relative, (content, mode)) in enumerate(plan.items()):
                candidate = stage / str(index)
                candidate.write_bytes(content)
                candidate.chmod(mode)
            for index, (relative, (content, mode)) in enumerate(plan.items()):
                target = _safe_path(paths, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = stage / (str(index) + ".old")
                if target.exists():
                    shutil.copy2(target, backup)
                os.replace(stage / str(index), target)
                committed.append((target, backup))
                installed[relative] = _digest(content)
            manifest = {"application": "Naui", "schema": 1, "prefix": str(paths.prefix),
                        "version": __version__, "files": installed}
            record = stage / "manifest"
            record.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            os.replace(record, paths.manifest)
        except BaseException:
            for target, backup in reversed(committed):
                if backup.exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    refresh_desktop(paths)
    return paths.launcher


def uninstall(paths):
    files = _read_manifest(paths)
    if not files:
        return []
    preserved = {}
    parents = set()
    for relative, digest in files.items():
        try:
            target = _safe_path(paths, relative)
        except ValueError:
            preserved[relative] = digest
            continue
        if not target.exists():
            continue
        if not target.is_file() or _digest(target.read_bytes()) != digest:
            preserved[relative] = digest
            continue
        target.unlink()
        parents.update(parent for parent in target.parents if parent == paths.app or paths.app in parent.parents)
    if preserved:
        record = {"application": "Naui", "schema": 1, "prefix": str(paths.prefix),
                  "version": __version__, "files": preserved}
        paths.manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    else:
        paths.manifest.unlink()
        parents.add(paths.app)
    for directory in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()  # Only empty directories; preserve config/unrelated files.
        except OSError:
            pass
    refresh_desktop(paths)
    return list(preserved)


def refresh_desktop(paths):
    for executable, arguments, directory in (
            ("update-desktop-database", [], paths.prefix / "share/applications"),
            ("gtk-update-icon-cache", ["-f", "-t"], paths.prefix / "share/icons/hicolor")):
        tool = shutil.which(executable)
        if tool and directory.is_dir():
            subprocess.run([tool, *arguments, str(directory)], check=False, timeout=30)


DEPENDENCY_CHECK = """
import gi
for name, version in [('Gtk','4.0'), ('Gdk','4.0'), ('Vte','3.91'), ('GtkSource','5'), ('GdkPixbuf','2.0'), ('Graphene','1.0')]:
    gi.require_version(name,version)
"""
APT_PACKAGES = ["python3", "python3-gi", "gir1.2-gtk-4.0", "gir1.2-vte-3.91", "gir1.2-gtksource-5",
                "gir1.2-gdkpixbuf-2.0", "webp-pixbuf-loader", "openssh-client", "libgtk-4-media-gstreamer",
                "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good", "gstreamer1.0-libav"]
DNF_PACKAGES = ["python3", "python3-gobject", "gtk4", "vte291-gtk4", "gtksourceview5", "gdk-pixbuf2",
                "webp-pixbuf-loader", "openssh-clients", "gstreamer1-plugins-base", "gstreamer1-plugins-good"]


def main(source, argv=None):
    parser = argparse.ArgumentParser(description="Naui user installer/uninstaller (Linux)")
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--prefix", help="Installation prefix; defaults to ~/.local")
    parser.add_argument("--python", default=shutil.which("python3") or sys.executable)
    parser.add_argument("--install-deps", action="store_true", help="Install system dependencies with apt-get/dnf and sudo")
    parser.add_argument("--skip-dependency-check", action="store_true", help="Package files without checking GUI libraries")
    args = parser.parse_args(argv)
    if sys.platform == "darwin":
        parser.exit(1, "macOS support is planned; no tested Naui.app exists. See packaging/macos/README.md.\n")
    if not sys.platform.startswith("linux"):
        parser.exit(1, "Naui currently installs on Linux only.\n")
    default_prefix = None
    source_manifest = Path(source) / ".naui-install.json"
    if args.action == "uninstall" and args.prefix is None and source_manifest.is_file():
        default_prefix = Path(source).parents[1]
    try:
        paths = InstallPaths.make(args.prefix or default_prefix)
        if args.action == "install":
            python = Path(args.python).expanduser().resolve()
            if not python.is_file() or not os.access(python, os.X_OK):
                raise ValueError("Python executable not found")
            if args.install_deps:
                if shutil.which("apt-get"):
                    subprocess.run(["sudo", "apt-get", "install", "-y", *APT_PACKAGES], check=True)
                elif shutil.which("dnf"):
                    subprocess.run(["sudo", "dnf", "install", "-y", *DNF_PACKAGES], check=True)
                else:
                    raise ValueError("Install GTK 4/VTE GTK4/GtkSourceView/PyGObject/OpenSSH using your distribution's package manager")
            if not args.skip_dependency_check:
                check = subprocess.run([str(python), "-c", DEPENDENCY_CHECK], capture_output=True, text=True)
                if check.returncode or not shutil.which("ssh"):
                    raise ValueError("Missing GUI/OpenSSH dependencies. Run ./install.sh --install-deps --python /usr/bin/python3, or see README.md.\n" + check.stderr.strip())
            launcher = install(source, paths, python)
            print("Installed Naui " + __version__ + " at " + str(launcher))
            print("Desktop launcher: " + str(paths.prefix / "share/applications/naui.desktop"))
            from .sshfs import detect_support
            support = detect_support()
            if not support.usable:
                print('Optional SSHFS unavailable: ' + support.reason + '. Naui will use compatibility remote file access.')
            if str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
                print('Add this directory to PATH in your shell: export PATH=' + shlex.quote(str(launcher.parent)) + ':"$PATH"')
        else:
            preserved = uninstall(paths)
            print("Naui uninstall complete. User configuration/cache and unrelated files were preserved.")
            for relative in preserved:
                print("Preserved modified/unowned file: " + str(paths.prefix / relative))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, "Naui: " + str(error) + "\n")

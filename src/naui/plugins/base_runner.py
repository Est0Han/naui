"""Explicit file execution using public device/terminal APIs only."""
import posixpath
import shlex
from types import SimpleNamespace

from ..transfers import FileLocation
from ..navigation import navigation_path

INTERPRETERS = {'.py': 'python3', '.sh': 'bash', '.js': 'node'}


def runnable(entry):
    return entry.kind == 'f' and (posixpath.splitext(entry.path)[1].lower() in INTERPRETERS or bool(entry.mode & 0o111))


def file_command(path, mode, arguments=()):
    interpreter = INTERPRETERS.get(posixpath.splitext(path)[1].lower())
    if interpreter: return (interpreter, '--', path, *arguments)
    if mode & 0o111: return (path, *arguments)
    raise ValueError('This file has no supported interpreter or executable permission')


class Runner:
    def __init__(self, api):
        self.api, self.last_run = api, None
        for identity, label, new, arguments in [('run', 'Run', False, False),
                ('run-new', 'Run in New Terminal', True, False), ('run-args', 'Run With Arguments…', False, True)]:
            api.add_file_action(identity, label, runnable,
                lambda location, entry, new=new, arguments=arguments: self.run(location, entry, new, arguments))
        api.add_command('current', 'Run Current File', self.current, default_shortcut='Ctrl+F5', context='editor',
                        enabled=lambda: bool(api.current_document()))
        api.add_command('stop', 'Stop Runner', self.stop_run, default_shortcut='Shift+F5', enabled=lambda: bool(self.last_run))

    def current(self):
        document = self.api.current_document()
        if document:
            def run():
                if posixpath.splitext(document.location.path)[1].lower() in INTERPRETERS:
                    # Known text interpreters need no stat prerequisite. Core
                    # can reconnect this document's device and await its PTY.
                    self.run(document.location, SimpleNamespace(mode=0o600))
                else:
                    def inspected(entry, error):
                        if error: self.api.message(str(error))
                        elif runnable(entry): self.run(document.location, entry)
                        else: self.api.message('File is not runnable')
                    self.api.properties(document.location, inspected)
            if document.dirty:
                self.api.confirm('Run saved version?', 'This editor has unsaved changes. Run uses the file currently saved on its device.',
                                 'Run Saved File', run)
            else: run()

    def run(self, location, entry, new=False, arguments=False):
        def launch(argv=(), cwd=None):
            try:
                cwd = cwd or posixpath.dirname(location.path)
                self.last_run = self.api.terminals.run(device_id=location.device_id, cwd=cwd,
                    command=file_command(location.path, entry.mode, argv), new_terminal=new)
            except (ValueError, RuntimeError, OSError) as error: self.api.message(str(error))
        if arguments:
            def entered(values):
                try:
                    device = next(item for item in self.api.devices() if item.id == location.device_id)
                    cwd = navigation_path(values[1], posixpath.dirname(location.path), device.home)
                    launch(shlex.split(values[0]), cwd)
                except (ValueError, StopIteration) as error: self.api.message(str(error))
            self.api.input('Run ' + posixpath.basename(location.path),
                           [('Arguments', ''), ('Working directory', posixpath.dirname(location.path))], entered)
        else: launch()

    def stop_run(self):
        if self.last_run: self.api.terminals.interrupt(self.last_run)


def start(api): return Runner(api)

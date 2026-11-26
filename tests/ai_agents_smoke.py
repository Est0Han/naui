#!/usr/bin/env python3
"""Real GTK/VTE agent tasks without paid calls; preserves desktop runtime.

PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings
python3 tests/ai_agents_smoke.py
"""
import gc
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import gi

for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('Vte', '3.91'), ('GtkSource', '5')]:
    gi.require_version(name, version)
from gi.repository import Gdk, Gtk
from naui.gui import NauiApplication
from naui.lifecycle import Sources
from naui.plugin_sdk import scaffold, agent_prompt
from naui.plugin_manifest import fingerprint
from naui.plugin_storage import PluginStore


def acceptance(root, *, safe_mode=False):
    app, sources, errors = NauiApplication([], str(root)), Sources(), []
    app.safe_mode = safe_mode
    state = dict(stage='start', cycles=0)
    deadline = time.monotonic() + 180
    old_hook = sys.excepthook
    def exception(kind, value, traceback):
        errors.append(value); old_hook(kind, value, traceback)
    sys.excepthook = exception
    def tick():
        window = app.window
        try:
            if errors: raise errors[0]
            assert time.monotonic() < deadline, state
            assert not window._shutting_down
            if state['stage'] == 'start':
                window.show_plugins(); state['view'] = window.plugin_manager_view
                assert any(spec.bundled for spec, _ in window.plugins.plugins.values())
                state['primary'] = next(iter(window.terminals))
                if safe_mode:
                    assert 'broken-agent' not in window.plugins.plugins
                    assert window.plugins.catalog.restore_candidates() == []
                    assert 'SAFE MODE' in window.get_title()
                    window.plugins.catalog.disable('broken-agent')
                    window.plugins.catalog.uninstall('broken-agent')
                    state['stage'] = 'done'; sources.close(); window.close(); return
                settings = window.ai_agents.settings
                settings.save(dict(schema=1, default_provider='custom', default_model='', custom={
                    'name':'Harmless mock', 'executable':str(root/'mock-agent'), 'arguments':[], 'delivery':'stdin'}))
                state['stage'] = 'launch'
            view = state['view']
            if state['stage'] == 'launch':
                plugin = view.catalog.store.directory/'hello-agent'
                (plugin/'result.json').unlink(missing_ok=True)
                state['previous'] = set(window.ai_agents.tasks)
                view.agents.start('hello-agent', 'custom', agent_prompt(plugin, 'Harmless mock task', []))
                state['stage'] = 'wait-start'
            elif state['stage'] == 'wait-start' and not view.busy:
                added = set(window.ai_agents.tasks) - state['previous']
                assert len(added) == 1, view.status.get_text()
                state['task'] = window.ai_agents.tasks[added.pop()]
                state['stage'] = 'complete'
            elif state['stage'] == 'complete':
                task = state['task']
                if task.state == 'failed': raise AssertionError(task.error)
                if task.state != 'completed': sources.timeout(50, tick); return
                result = json.loads((Path(task.cwd)/'result.json').read_text())
                assert result['cwd'] == task.cwd and result['tty']
                assert task.cwd in result['prompt'] and 'PLUGIN_API_CAPABILITY_MISSING:' in result['prompt']
                assert not {'NAUI_TOKEN', 'NAUI_LOCAL_KEY', 'SSH_AUTH_SOCK', 'OPENAI_API_KEY'} & result['env'].keys()
                assert task.cleaned and not Path(task.runtime.name).exists()
                assert task.terminal_id in window.terminals  # Output retained for inspection.
                view.agents.open_terminal(task)
                window.close_terminal(task.terminal_id, True)
                state['stage'] = 'closed'
            elif state['stage'] == 'closed' and state['task'].terminal_id not in window.terminals:
                gc.collect()
                primary = window.terminals[state['primary']]
                assert not primary.closed
                marker = root/'survivor'
                marker.unlink(missing_ok=True)
                primary.send_input(('printf alive > ' + shlex.quote(str(marker)) + '\n').encode())
                state['stage'] = 'survivor'
            elif state['stage'] == 'survivor' and (root/'survivor').exists():
                assert (root/'survivor').read_text() == 'alive'
                state['cycles'] += 1
                if state['cycles'] < 50: state['stage'] = 'launch'
                else:
                    # Cancel one fully started task through its own controls.
                    (root/'wait').touch()
                    state['previous'] = set(window.ai_agents.tasks)
                    view.agents.start('hello-agent', 'custom', 'Cancellation test')
                    state['stage'] = 'cancel-start'
            elif state['stage'] == 'cancel-start' and not view.busy:
                task = state['task'] = window.ai_agents.tasks[(set(window.ai_agents.tasks)-state['previous']).pop()]
                if not (root/'wait-ready').exists(): sources.timeout(50, tick); return
                window.ai_agents.stop_task(task.id); window.ai_agents.stop_task(task.id)
                state['stage'] = 'cancelled'
            elif state['stage'] == 'cancelled' and state['task'].terminal_id not in window.terminals:
                assert state['task'].state == 'cancelled' and state['task'].cleaned
                # Leave another task live during whole-application shutdown.
                state['previous'] = set(window.ai_agents.tasks)
                (root/'wait-ready').unlink()
                view.agents.start('hello-agent', 'custom', 'Shutdown test')
                state['stage'] = 'shutdown'
            elif state['stage'] == 'shutdown' and not view.busy and (root/'wait-ready').exists():
                state['shutdown_task'] = window.ai_agents.tasks[(set(window.ai_agents.tasks)-state['previous']).pop()]
                state['stage'] = 'done'; sources.close(); window.close(); return
        except Exception as error:
            errors.append(error); sources.close(); window._discard_approved = True; window.close(); return
        sources.timeout(50, tick)
    handler = app.connect_after('activate', lambda *_: sources.timeout(50, tick))
    try: result = app.run([sys.argv[0]])
    finally:
        sources.close(); app.disconnect(handler); sys.excepthook = old_hook
    if errors: raise errors[0]
    assert state['stage'] == 'done' and result == 0, (state, result)
    if not safe_mode:
        task = state['shutdown_task']
        assert task.process.poll() is not None and task.cleaned and task.state == 'cancelled'
    del state, app
    gc.collect()


def main():
    Gtk.init()
    if Gdk.Display.get_default() is None:
        print('SKIP: no accessible display; native AI agent acceptance was not run.')
        return 77
    with tempfile.TemporaryDirectory(prefix='naui-ai-gui-') as directory:
        root = Path(directory)
        for name in ('config', 'cache'): (root/name).mkdir(mode=0o700)
        os.environ.update(XDG_CONFIG_HOME=str(root/'config'), XDG_CACHE_HOME=str(root/'cache'), SHELL='/bin/bash')
        plugins = root/'config/naui/plugins'
        scaffold(plugins, 'hello-agent', 'Hello Agent', 'Harmless local mock', [])
        mock = root/'mock-agent'
        mock.write_text('#!' + sys.executable + '\n' + '''import json,os,sys,time
from pathlib import Path
prompt = sys.stdin.read()
Path('result.json').write_text(json.dumps({'cwd':os.getcwd(),'prompt':prompt,'env':dict(os.environ),'tty':os.isatty(1)}))
root = Path(__file__).parent
if (root/'wait').exists():
    (root/'wait-ready').touch()
    time.sleep(60)
''')
        mock.chmod(0o700)
        acceptance(root)
        broken = scaffold(plugins, 'broken-agent', 'Broken Agent', '', [])
        (broken/'plugin.py').write_text("raise RuntimeError('safe mode must not import this')\n")
        store = PluginStore(plugins)
        store.remember('broken-agent', True, [], fingerprint(broken))
        acceptance(root, safe_mode=True)
    print('PASS: 50 real agent PTY/VTE completions/tab closes with survivor input and GC; '
          'cancellation, active-task application shutdown, safe mode recovery; exit 0')
    return 0


if __name__ == '__main__': sys.exit(main())

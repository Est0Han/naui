"""Isolated interpreter for explicitly trusted imports/tests; NOT a sandbox."""
import sys
import inspect
import unittest
from pathlib import Path
from .plugin_manifest import inspect_manifest
from .plugin_validation import load_module, unload_module


def main():
    root = Path(sys.argv[1]).resolve()
    spec, entry, _ = inspect_manifest(root)
    module, namespace = load_module(root, entry)
    try:
        if 'Plugin' in module.__dict__:
            instance = module.Plugin()
            if not callable(getattr(instance, 'activate', None)) or not callable(getattr(instance, 'deactivate', None)):
                raise ValueError('Plugin must provide activate(api) and deactivate()')
            inspect.signature(instance.activate).bind(None)
            inspect.signature(instance.deactivate).bind()
        elif not callable(getattr(module, 'start', None)):
            raise ValueError('Missing callable start(api)')
        else:
            inspect.signature(module.start).bind(None)
        tests = root/'tests'
        if tests.is_dir():
            sys.path.insert(0, str(root))
            suite = unittest.defaultTestLoader.discover(str(tests), pattern='test*.py')
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            if not result.wasSuccessful(): return 1
            print(f'{result.testsRun} plugin tests passed')
        else: print('No tests directory; add unittest tests for behavior')
        return 0
    finally: unload_module(namespace)


if __name__ == '__main__': raise SystemExit(main())

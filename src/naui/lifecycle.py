"""Main-loop sources and file jobs with explicit, main-thread ownership.

Workers must receive only backend objects and plain data, never GTK objects.
UI completion callbacks live here until delivery/cancellation on the UI thread;
they are deliberately NOT captured by Future.add_done_callback().
"""
from concurrent.futures import ThreadPoolExecutor
import threading

from gi.repository import GLib


class Sources:
    def __init__(self):
        self._lock = threading.RLock()
        self._ids = set()
        self._closed = False

    def _add(self, add, callback, args):
        with self._lock:
            if self._closed:
                return None
            source_id = None

            def dispatch():
                with self._lock:
                    self._ids.discard(source_id)
                    if self._closed:
                        return False
                callback(*args)
                return False

            source_id = add(dispatch)
            self._ids.add(source_id)
            return source_id

    def idle(self, callback, *args):
        return self._add(GLib.idle_add, callback, args)

    def timeout(self, milliseconds, callback, *args):
        return self._add(lambda dispatch: GLib.timeout_add(milliseconds, dispatch), callback, args)

    def remove(self, source_id):
        with self._lock:
            if source_id in self._ids:
                self._ids.remove(source_id)
                GLib.source_remove(source_id)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for source_id in self._ids:
                GLib.source_remove(source_id)
            self._ids.clear()


class FileWork:
    def __init__(self, sources, max_workers=4):
        self.sources = sources
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="naui-files")
        self._lock = threading.RLock()
        self._closed = False
        self._next_id = 0
        self._callbacks, self._futures, self._results = {}, {}, {}

    def submit(self, function, callback):
        # Called only by the UI thread. A worker completion owns only this
        # manager and an integer; GTK closures stay in _callbacks on the UI side.
        with self._lock:
            if self._closed:
                return None
            self._next_id += 1
            job = self._next_id
            self._callbacks[job] = callback
            future = self.executor.submit(function)
            self._futures[job] = future
            future.add_done_callback(lambda result: self._finished(job, result))
            return job

    def _finished(self, job, future):
        try:
            value, error = future.result(), None
        except Exception as exc:
            value, error = None, exc.with_traceback(None)
        with self._lock:
            self._futures.pop(job, None)
            if self._closed:
                return
            self._results[job] = value, error
            self.sources.idle(self._deliver, job)

    def _deliver(self, job):
        with self._lock:
            if self._closed:
                return
            callback = self._callbacks.pop(job, None)
            result = self._results.pop(job, None)
        if callback is not None and result is not None:
            callback(*result)

    def stop(self):
        # UI callbacks (and any GTK wrappers they retain) are released on the
        # main thread BEFORE a running worker is allowed to finish.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._callbacks.clear()
            self._results.clear()
            for future in list(self._futures.values()):
                future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def join(self):
        # Remote backends have already stopped/reaped their OpenSSH children.
        # Finish in-flight local atomic operations before application teardown.
        self.executor.shutdown(wait=True, cancel_futures=True)


class Signals:
    """Disconnect application-owned callbacks before their native owner disposes."""
    def __init__(self):
        self._handlers = []

    def connect(self, obj, name, callback):
        handler = obj.connect(name, callback)
        self._handlers.append((obj, handler))
        return handler

    def close(self):
        for obj, handler in self._handlers:
            if obj.handler_is_connected(handler):
                obj.disconnect(handler)
        self._handlers.clear()

    def disconnect(self, owner):
        remaining = []
        for obj, handler in self._handlers:
            if obj is owner:
                if obj.handler_is_connected(handler):
                    obj.disconnect(handler)
            else:
                remaining.append((obj, handler))
        self._handlers = remaining

"""Bounded synchronous reaping of locally owned OpenSSH/helper processes."""
import subprocess


def stop_process(process, grace=1.0):
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    process.wait()

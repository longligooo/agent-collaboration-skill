"""Thread-local cooperative budgets for UI background reads only.

A budget never interrupts an OS filesystem call. It bounds work between reads and
subprocess waits, and callers must retain capacity until the worker really exits.
CLI operations outside a budget retain their existing behavior.
"""
from contextlib import contextmanager
import subprocess
import threading
import time

_local = threading.local()


class ReadTimeout(RuntimeError):
    pass


def check():
    deadline = getattr(_local, 'deadline', None)
    if deadline is not None and time.monotonic() >= deadline:
        raise ReadTimeout('snapshot read deadline exceeded')


@contextmanager
def budget(deadline):
    previous = getattr(_local, 'deadline', None)
    _local.deadline = min(previous, deadline) if previous is not None else deadline
    try:
        check()
        yield
        check()
    finally:
        _local.deadline = previous


def run(*args, **kwargs):
    deadline = getattr(_local, 'deadline', None)
    if deadline is not None:
        check()
        kwargs['timeout'] = min(kwargs.get('timeout', 10), 10, deadline - time.monotonic())
    try:
        return subprocess.run(*args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        if deadline is None:
            raise
        raise ReadTimeout('Git metadata read timed out') from exc

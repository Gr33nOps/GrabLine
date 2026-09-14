"""Single-flight guards for one-shot UI actions.

A button whose handler opens a dialog or starts a background task stays
clickable while that work is pending (a modal hasn't painted yet, or a network
task is in flight), so a double-click fires the handler twice - two update
dialogs, two FFmpeg installers. These keep the second invocation from doing
anything until the first has finished.

Two shapes, because the work has two shapes:

- ``single_flight`` (a context manager) for synchronous work like a modal
  ``dialog.exec()``: the key is held for the ``with`` block and released
  automatically, even on exception.
- ``begin``/``end`` for asynchronous work like a background thread: ``begin``
  when the task starts, ``end`` from its completion callback.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def single_flight(registry: set[str], key: str) -> Iterator[bool]:
    """Yield True the first time; yield False (doing nothing) while ``key`` is
    still running. Releases the key when the block exits."""
    if key in registry:
        yield False
        return
    registry.add(key)
    try:
        yield True
    finally:
        registry.discard(key)


def begin(registry: set[str], key: str) -> bool:
    """Claim ``key`` for an async task. True if claimed, False if already
    running (the caller should return without starting a second one)."""
    if key in registry:
        return False
    registry.add(key)
    return True


def end(registry: set[str], key: str) -> None:
    """Release ``key`` once its async task has finished (call from every
    completion path - success and failure)."""
    registry.discard(key)

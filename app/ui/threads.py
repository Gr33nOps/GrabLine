"""Keep a running QThread alive until it actually finishes.

A QThread owned only by a dialog is destroyed the moment that dialog closes.
If its worker is still running - a slow thumbnail fetch, a URL inspection, an
FFmpeg download - Qt aborts the whole process:

    QThread: Destroyed while thread '' is still running

which is a SIGABRT with no Python traceback. Handing the thread to ``retain``
moves ownership out of the dialog: this module holds the reference until the
thread emits ``finished``, then releases it and schedules ``deleteLater``. The
dialog may close whenever it likes; the thread lives exactly as long as its
work does, and is destroyed only once it has genuinely stopped.

The thread must be unparented (or at least not parented to the dialog) so a
closing dialog cannot destroy it out from under this registry.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

#: Threads currently running on someone's behalf. A module-level strong
#: reference is what keeps the QThread's C++ object alive past the death of
#: whatever UI started it.
_RUNNING: set[QThread] = set()


class CallableThread(QThread):
    """Run a callable off the GUI thread and emit its result on ``done``.

    For work that returns a result object and does not raise - a report whose
    failure is encoded in the object itself (``reachable=False``), not a
    thrown exception. There is no error channel by design; pair with
    :func:`retain` so a closing dialog never destroys it mid-run.
    """

    done = Signal(object)

    def __init__(self, work: Callable[[], object]) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        self.done.emit(self._work())


def retain(thread: QThread) -> None:
    """Own ``thread`` until it finishes, then release and delete it. Call this
    immediately before ``thread.start()``."""
    _RUNNING.add(thread)

    def _release() -> None:
        _RUNNING.discard(thread)
        thread.deleteLater()

    thread.finished.connect(_release)


def shutdown(timeout_ms: int = 6000) -> None:
    """On app quit, wait for retained threads to finish before the interpreter
    tears them down. Destroying a still-running QThread aborts the process, so
    the alternative to waiting is a crash on exit. Retained workers use bounded
    network timeouts, so a normal quit returns at once; a quit during a stuck
    fetch waits at most ``timeout_ms``. Any thread that still hasn't stopped is
    left referenced on purpose - a leaked-but-alive thread is safe; a
    destroyed-while-running one is not."""
    import time

    deadline = time.monotonic() + timeout_ms / 1000
    for thread in list(_RUNNING):
        thread.requestInterruption()
    for thread in list(_RUNNING):
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if thread.wait(remaining):
            continue
        # Still blocked past the budget - a worker stuck in a synchronous
        # network call it can't check interruption during. The process is
        # exiting; forcibly stop it rather than let the interpreter destroy a
        # running QThread (which aborts). terminate() is unsafe mid-operation
        # in general, but at shutdown there is nothing left to corrupt.
        thread.terminate()
        thread.wait(1000)

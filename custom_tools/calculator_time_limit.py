"""Run the calculator's CPU-bound work with a time limit inside the agent process.

Why this shape: tools run inside the agent process, which must not start other
processes for them, and Python cannot kill a thread. So the work runs in its
own thread; when the time is up the caller gets an error at once, and a small
stopper thread raises ``CalculationStopped`` inside the work thread with
``PyThreadState_SetAsyncExc`` (documented CPython C API). Python delivers it
between two steps of Python code, so long SymPy algorithms stop promptly. It
repeats the request until the work thread ends, because some library code
(mpmath) catches every exception with a bare ``except``.

Limits: one exact-number step running in C (a single huge multiplication)
cannot be interrupted; ``custom_tools.calculator_parsing`` refuses the inputs
that ask for one. Only one calculation runs per process at a time (``_SLOT``),
so a calculation that is still stopping delays the next one instead of piling
up CPU work; a caller waits up to one time limit for its turn.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

logger = logging.getLogger(__name__)

T = TypeVar("T")
STOP_RETRY_SECONDS = 0.2
_SLOT = threading.BoundedSemaphore(1)


class CalculationStopped(BaseException):
    """Raised inside the work thread to stop it.

    A BaseException, so SymPy's ``except Exception`` handlers do not swallow it.
    """


class CalculationTimedOut(Exception):
    """The work did not finish within its time limit (it is being stopped)."""


class CalculatorBusy(Exception):
    """An earlier calculation in this process has not finished stopping."""


@dataclass
class _Outcome:
    value: Any = None
    error: Exception | None = None
    stopped: bool = False


class _Job:
    """One calculation and its stop handshake.

    ``finished`` is set by the work thread once it accepts no more stop
    requests; ``stopping`` is set by the stopper while it decides whether to
    send one. The work thread sets ``finished`` and then waits while
    ``stopping`` is set, so a request is either not sent or arrives while the
    work thread is still inside its ``try``. (A lock cannot do this: a request
    arriving just after the lock is taken would leave it held.)
    """

    def __init__(self, work: Callable[[], Any]) -> None:
        self.work = work
        self.started = False
        self.finished = False
        self.stopping = False
        self.outcome: _Outcome | None = None
        self.done = threading.Event()


def _attempt(work: Callable[[], Any]) -> _Outcome:
    try:
        return _Outcome(value=work())
    except CalculationStopped:
        return _Outcome(stopped=True)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, which turns it into the tool's error
        return _Outcome(error=exc)


def _checkpoint() -> None:
    """A Python call: a pending stop request is raised here, inside the caller's ``try``."""


def _run(job: _Job) -> None:
    """The work thread. A stop request can arrive at any step, so each step is safe to retry."""

    try:
        while True:
            try:
                if not job.started:
                    job.started = True
                    job.outcome = _attempt(job.work)
                job.finished = True
                while job.stopping:
                    time.sleep(0.001)
                _checkpoint()
                break
            except CalculationStopped:
                job.outcome = job.outcome or _Outcome(stopped=True)
    finally:
        _SLOT.release()
        job.done.set()


def _raise_in_thread(thread_id: int) -> None:
    changed = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), ctypes.py_object(CalculationStopped))
    if changed > 1:  # cannot happen for a live thread id; undo rather than hit other threads
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)


def _stop(job: _Job, thread_id: int) -> None:
    """The stopper thread: ask the work thread to stop until it has ended (see ``_Job``)."""

    while True:
        job.stopping = True
        try:
            if job.finished:
                return
            _raise_in_thread(thread_id)
        finally:
            job.stopping = False
        if job.done.wait(STOP_RETRY_SECONDS):
            return


def run_with_time_limit(work: Callable[[], T], *, seconds: float) -> T:
    """Return ``work()`` or raise its exception; CalculationTimedOut or CalculatorBusy otherwise."""

    if not _SLOT.acquire(timeout=seconds):
        raise CalculatorBusy("An earlier calculation is still stopping. Try again shortly.")
    job = _Job(work)
    thread = threading.Thread(target=_run, args=(job,), name="calculator", daemon=True)
    try:
        thread.start()
    except BaseException:
        _SLOT.release()
        raise
    if not job.done.wait(seconds):
        thread_id = thread.ident or 0  # set once started
        threading.Thread(target=_stop, args=(job, thread_id), name="calculator-stop", daemon=True).start()
        raise CalculationTimedOut(f"The calculation took longer than {seconds:g} seconds and was stopped.")
    outcome = job.outcome or _Outcome(stopped=True)
    if outcome.error is not None:
        raise outcome.error
    if outcome.stopped:
        raise CalculationTimedOut(f"The calculation took longer than {seconds:g} seconds and was stopped.")
    return cast(T, outcome.value)


__all__ = ["CalculationTimedOut", "CalculatorBusy", "run_with_time_limit"]

"""Persistent supervisor loop for queued diagnosis and coding tasks."""
from __future__ import annotations

import fcntl
import signal
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

from coding_worker import run_next_coding_task
from diagnostic_worker import run_next_diagnostic
from task_queue import Task, TaskQueue


class SupervisorAlreadyRunning(RuntimeError):
    pass


@contextmanager
def _supervisor_lock(queue: TaskQueue) -> Iterator[None]:
    lock_path = queue.path.with_suffix(queue.path.suffix + ".supervisor.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SupervisorAlreadyRunning(
                f"Another supervisor already owns queue: {queue.path}"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_next_task(queue: TaskQueue) -> Task | None:
    """Claim and execute one eligible task, dispatching by its persisted kind."""
    task = queue.claim_next()
    if task is None:
        return None
    if task.kind == "code":
        return run_next_coding_task(queue, claimed_task=task)
    return run_next_diagnostic(queue, claimed_task=task)


def _run_supervisor_locked(
    queue: TaskQueue,
    *,
    poll_interval: float = 5.0,
    max_tasks: int | None = None,
    stop_event: threading.Event | None = None,
    recover: bool = True,
    on_task: Callable[[Task], None] | None = None,
) -> int:
    """Poll until signalled or max_tasks tasks have been attempted.

    A single supervisor should own a queue. Interrupted tasks are recovered once
    at startup; the loop never resets running tasks while it is active.
    """
    if poll_interval < 0.1:
        raise ValueError("poll_interval must be at least 0.1 seconds.")
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1 when specified.")
    stop = stop_event or threading.Event()
    if recover:
        queue.recover_interrupted()
    processed = 0
    while not stop.is_set():
        control = queue.runtime_control()
        if control.get("cancel_requested"):
            break
        if control.get("paused"):
            stop.wait(poll_interval)
            continue
        task = run_next_task(queue)
        if task is None:
            stop.wait(poll_interval)
            continue
        processed += 1
        if on_task:
            on_task(task)
        if max_tasks is not None and processed >= max_tasks:
            break
    return processed


def run_supervisor(
    queue: TaskQueue, *, poll_interval: float = 5.0, max_tasks: int | None = None,
    stop_event: threading.Event | None = None, recover: bool = True,
    on_task: Callable[[Task], None] | None = None,
) -> int:
    stop = stop_event or threading.Event()
    if stop.is_set():
        return 0
    with _supervisor_lock(queue):
        return _run_supervisor_locked(
            queue, poll_interval=poll_interval, max_tasks=max_tasks,
            stop_event=stop, recover=recover, on_task=on_task,
        )

def install_signal_handlers(stop_event: threading.Event) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers; restore the original handlers on exit."""
    previous = {}

    def request_stop(signum, frame):
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)

    def restore():
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore

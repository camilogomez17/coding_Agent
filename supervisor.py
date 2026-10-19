"""Persistent supervisor loop for queued diagnosis and coding tasks."""
from __future__ import annotations

import signal
import threading
from typing import Callable

from coding_worker import run_next_coding_task
from diagnostic_worker import run_next_diagnostic
from task_queue import Task, TaskQueue


def run_next_task(queue: TaskQueue) -> Task | None:
    """Execute the oldest currently eligible task, dispatching by its saved kind."""
    tasks = queue.list_tasks(status="pending")
    by_id = {task.id: task for task in queue.list_tasks()}
    candidate = None
    for task in tasks:
        if task.depends_on:
            prerequisite = by_id.get(task.depends_on)
            if prerequisite and prerequisite.status != "succeeded":
                continue
        candidate = task
        break
    if candidate is None:
        # Let the queue mark tasks with failed/missing prerequisites as blocked.
        for task in tasks:
            result = queue.claim_next(kind=task.kind)
            if result is not None:
                return _execute_claimed(queue, result)
        return None
    if candidate.kind == "code":
        return run_next_coding_task(queue)
    return run_next_diagnostic(queue)


def _execute_claimed(queue: TaskQueue, task: Task) -> Task:
    """Record a clear failure for a claimed task when no dispatcher handles it."""
    return queue.fail(task.id, f"Unsupported task kind: {task.kind}", retry=False)


def run_supervisor(
    queue: TaskQueue,
    *,
    poll_interval: float = 5.0,
    max_tasks: int | None = None,
    stop_event: threading.Event | None = None,
    recover: bool = True,
    on_task: Callable[[Task], None] | None = None,
) -> int:
    """Poll continuously until signalled or max_tasks tasks have been attempted.

    A single supervisor should own a queue. Interrupted running tasks are recovered
    once at startup; the loop never resets running tasks while it is active.
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


def install_signal_handlers(stop_event: threading.Event) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers that stop after the active task finishes."""
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

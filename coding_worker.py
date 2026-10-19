"""Run one queued task through the restricted coding engine."""
from __future__ import annotations

from typing import Any

from coding_engine import CodingEngineError, run_coding_task
from model_client import ModelClient
from task_queue import Task, TaskQueue


def run_next_coding_task(
    queue: TaskQueue,
    model_client: Any | None = None,
    *,
    max_attempts: int = 2,
) -> Task | None:
    """Claim one task, run the bounded coding engine, and persist its outcome."""
    task = queue.claim_next()
    if task is None:
        return None
    try:
        result = run_coding_task(
            task.workspace,
            task.prompt,
            model_client=model_client or ModelClient(),
            max_attempts=max_attempts,
        )
        report = (
            f"Summary: {result.summary}\n"
            f"Branch: {result.branch}\n"
            f"Commit: {result.commit}\n"
            f"Changed files: {', '.join(result.changed_files)}\n"
            f"Engine attempts: {result.attempts}\n"
            f"Verification: {result.test_output}"
        )
        return queue.complete(task.id, result=report)
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
        return queue.fail(task.id, reason[:1000], retry=True)

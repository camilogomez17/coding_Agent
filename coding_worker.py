"""Run one queued task through the restricted coding engine."""
from __future__ import annotations

from typing import Any

from coding_engine import run_coding_task
from git_workflow import GitPublishError, publish_branch
from model_client import ModelClient
from planner import adapt_completed_step
from task_queue import Task, TaskQueue
from workspace_isolation import isolated_worktree


def run_next_coding_task(
    queue: TaskQueue,
    model_client: Any | None = None,
    *,
    max_attempts: int = 2,
    claimed_task: Task | None = None,
) -> Task | None:
    """Run one coding task in a disposable worktree and persist its outcome."""
    task = claimed_task or queue.claim_next(kind="code")
    if task is None:
        return None
    try:
        with isolated_worktree(task.workspace) as (worktree, base_branch):
            checkpoint_saved = [False]

            def checkpoint_verified_commit(verified_result):
                report = (
                    f"Summary: {verified_result.summary}\n"
                    f"Branch: {verified_result.branch}\n"
                    f"Base branch: {verified_result.base_branch}\n"
                    f"Commit: {verified_result.commit}\n"
                    f"Changed files: {', '.join(verified_result.changed_files)}\n"
                    f"Engine attempts: {verified_result.attempts}\n"
                    f"Verification: {verified_result.test_output}"
                )
                queue.annotate_task(task.id, report, action="verified_commit")
                checkpoint_saved[0] = True

            result = run_coding_task(
                worktree,
                task.prompt,
                model_client=model_client or ModelClient(),
                max_attempts=max_attempts,
                allowed_paths=task.allowed_paths or None,
                base_branch_override=base_branch,
                on_verified_commit=checkpoint_verified_commit,
            )
            report = (
                f"Summary: {result.summary}\n"
                f"Branch: {result.branch}\n"
                f"Base branch: {result.base_branch}\n"
                f"Commit: {result.commit}\n"
                f"Changed files: {', '.join(result.changed_files)}\n"
                f"Engine attempts: {result.attempts}\n"
                f"Verification: {result.test_output}"
            )
            # Fall back to a checkpoint if the immediate callback could not persist it.
            if not checkpoint_saved[0]:
                queue.annotate_task(task.id, report, action="verified_commit")
            if task.publish:
                try:
                    title = result.summary.strip() or task.prompt.strip()
                    pull_request_url = publish_branch(
                        worktree,
                        branch=result.branch,
                        base_branch=result.base_branch,
                        title=f"Agent: {title}",
                        summary=result.summary,
                        commit=result.commit,
                        changed_files=result.changed_files,
                    )
                    report += f"\nPull request: {pull_request_url}"
                except GitPublishError as publish_error:
                    report += f"\nPublish failed: {publish_error}"
        completed = queue.complete(task.id, result=report)
        if task.adaptive_plan:
            try:
                revised = adapt_completed_step(
                    queue, completed, report, model_client=model_client
                )
                note = f"Adaptive replanning: {len(revised)} remaining step(s) queued."
                queue.annotate_task(task.id, note, action="plan_revised")
            except Exception as planning_error:
                queue.annotate_task(
                    task.id,
                    "Adaptive replanning failed; the existing queued plan remains "
                    f"unchanged: {type(planning_error).__name__}: {planning_error}",
                    action="plan_revision_failed",
                )
            completed = next(item for item in queue.list_tasks() if item.id == task.id)
        return completed
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
        return queue.fail(task.id, reason[:1000], retry=True)

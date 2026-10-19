import json

import pytest

from task_queue import QueueError, TaskQueue


def test_enqueue_persists_task_and_normalizes_workspace(tmp_path):
    queue = TaskQueue(tmp_path / "state" / "tasks.json")
    task = queue.enqueue("Inspect this project", tmp_path)

    assert task.status == "pending"
    assert task.attempts == 0
    assert task.workspace == str(tmp_path.resolve())

    reloaded = TaskQueue(tmp_path / "state" / "tasks.json")
    assert reloaded.list_tasks() == [task]


def test_claims_pending_tasks_in_order(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    first = queue.enqueue("First", tmp_path)
    second = queue.enqueue("Second", tmp_path)

    claimed = queue.claim_next()

    assert claimed.id == first.id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert queue.claim_next().id == second.id
    assert queue.claim_next() is None


def test_complete_records_success_and_result(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    task = queue.enqueue("Do work", tmp_path)
    queue.claim_next()

    completed = queue.complete(task.id, result="Finished safely")

    assert completed.status == "succeeded"
    assert completed.result == "Finished safely"
    assert queue.list_tasks(status="succeeded") == [completed]


def test_failure_retries_until_attempt_limit(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=2)
    task = queue.enqueue("Do work", tmp_path)

    first_attempt = queue.claim_next()
    retried = queue.fail(first_attempt.id, "Temporary error")
    assert retried.status == "pending"
    assert retried.attempts == 1

    second_attempt = queue.claim_next()
    failed = queue.fail(second_attempt.id, "Still broken")
    assert failed.status == "failed"
    assert failed.attempts == 2
    assert queue.claim_next() is None


def test_interrupted_task_is_requeued_after_restart(tmp_path):
    path = tmp_path / "tasks.json"
    queue = TaskQueue(path, max_attempts=3)
    task = queue.enqueue("Interrupted work", tmp_path)
    queue.claim_next()

    restarted = TaskQueue(path, max_attempts=3)
    assert restarted.recover_interrupted() == 1
    recovered = restarted.list_tasks()[0]

    assert recovered.id == task.id
    assert recovered.status == "pending"
    assert recovered.error == "Requeued after an interrupted run."


def test_interrupted_task_at_attempt_limit_is_failed(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=1)
    queue.enqueue("Interrupted work", tmp_path)
    queue.claim_next()

    assert queue.recover_interrupted() == 1
    task = queue.list_tasks()[0]
    assert task.status == "failed"
    assert "Retry limit reached" in task.error


def test_rejects_empty_prompt_and_invalid_workspace(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")

    with pytest.raises(QueueError, match="prompt cannot be empty"):
        queue.enqueue("   ", tmp_path)

    with pytest.raises(QueueError, match="not an existing directory"):
        queue.enqueue("Do work", tmp_path / "missing")


def test_cannot_complete_task_that_is_not_running(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    task = queue.enqueue("Do work", tmp_path)

    with pytest.raises(QueueError, match="must be running"):
        queue.complete(task.id, result="Too early")


def test_invalid_queue_file_is_not_silently_overwritten(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text("{broken json", encoding="utf-8")
    queue = TaskQueue(path)

    with pytest.raises(QueueError, match="unreadable or invalid JSON"):
        queue.enqueue("Do work", tmp_path)

    assert path.read_text(encoding="utf-8") == "{broken json"


def test_unknown_status_filter_is_rejected(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")

    with pytest.raises(QueueError, match="Unknown task status"):
        queue.list_tasks(status="waiting")



def test_publish_preference_persists_for_tasks_and_plans(tmp_path):
    path = tmp_path / "tasks.json"
    queue = TaskQueue(path)
    task = queue.enqueue(
        "Make a change", tmp_path, kind="code", publish=True,
        allowed_paths=["coding_engine.py", "tests/test_coding_engine.py"],
    )
    plan = queue.enqueue_plan("Build a feature", tmp_path, ["Step one", "Step two"], publish=True)

    reloaded = TaskQueue(path)
    tasks = reloaded.list_tasks()

    saved_task = next(item for item in tasks if item.id == task.id)
    assert saved_task.publish is True
    assert saved_task.allowed_paths == ["coding_engine.py", "tests/test_coding_engine.py"]
    plan_tasks = [item for item in tasks if item.plan_id == plan[0].plan_id]
    assert len(plan_tasks) == 2
    assert all(item.publish for item in plan_tasks)

def test_purge_failed_removes_terminal_failures_but_preserves_other_tasks(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=1)
    failed = queue.enqueue("Known failed task", tmp_path)
    queue.claim_next()
    queue.fail(failed.id, "Permanent failure", retry=False)

    successful = queue.enqueue("Successful task", tmp_path)
    queue.claim_next()
    queue.complete(successful.id, result="Done")

    assert queue.purge_failed() == 1
    remaining = queue.list_tasks()
    assert len(remaining) == 1
    assert remaining[0].id == successful.id


def test_purge_failed_refuses_to_break_live_task_dependencies(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=1)
    plan = queue.enqueue_plan("Plan", tmp_path, ["First step", "Second step"])
    first = queue.claim_next()
    queue.fail(first.id, "First step failed", retry=False)

    with pytest.raises(QueueError, match="depend on them"):
        queue.purge_failed()

    tasks = queue.list_tasks()
    assert len(tasks) == 2
    assert tasks[0].status == "failed"
    assert tasks[1].status == "pending"
    assert tasks[1].depends_on == plan[0].id

def test_purge_failed_clears_saved_failure_details_from_retryable_tasks(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=2)
    task = queue.enqueue("Retryable task", tmp_path)
    queue.claim_next()
    retried = queue.fail(task.id, "Temporary failure", retry=True)
    assert retried.status == "pending"
    assert retried.error == "Temporary failure"
    assert any(event["action"] == "failed" for event in retried.history)

    assert queue.purge_failed() == 0
    cleaned = queue.list_tasks()[0]
    assert cleaned.status == "pending"
    assert cleaned.attempts == 1
    assert cleaned.error is None
    assert all(event["action"] != "failed" for event in cleaned.history)



def test_adaptive_plan_replaces_only_pending_tail_after_success(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    original = queue.enqueue_plan(
        "Improve parser", tmp_path, ["Inspect parser", "Implement fix", "Add tests"],
        adaptive=True, allowed_paths=["parser.py", "tests/test_parser.py"],
    )
    first = queue.claim_next(kind="code")
    assert first.id == original[0].id
    completed = queue.complete(first.id, result="Parser inspected; fix is smaller than expected.")

    revised = queue.revise_pending_plan(completed.id, ["Fix parser edge case", "Add focused regression test"])

    all_tasks = queue.list_tasks()
    assert [task.id for task in revised] == [task.id for task in all_tasks if task.status == "pending"]
    assert [task.step_index for task in revised] == [2, 3]
    assert revised[0].depends_on == completed.id
    assert revised[1].depends_on == revised[0].id
    assert all(task.adaptive_plan for task in revised)
    assert all(task.plan_objective == "Improve parser" for task in revised)
    assert all(task.allowed_paths == ["parser.py", "tests/test_parser.py"] for task in revised)
    assert all("Inspect parser" not in task.prompt for task in revised)


def test_adaptive_plan_can_finish_early_by_removing_remaining_steps(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    tasks = queue.enqueue_plan("Inspect app", tmp_path, ["Inspect", "Fix", "Test"], adaptive=True)
    queue.claim_next(kind="code")
    completed = queue.complete(tasks[0].id, result="Objective already satisfied.")

    assert queue.revise_pending_plan(completed.id, []) == []
    assert queue.list_tasks(status="pending") == []
    assert len(queue.list_tasks(status="succeeded")) == 1

import json
import threading

import pytest

import planner
import supervisor
from task_queue import QueueError, TaskQueue


class FakePlanner:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def generate(self, prompt, system=None):
        self.prompts.append((prompt, system))
        return self.response


def test_plan_creates_ordered_dependent_coding_tasks(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    model = FakePlanner(json.dumps({"steps": ["Add core behavior", "Add regression tests"]}))

    tasks = planner.plan_objective(queue, str(tmp_path), "Implement feature", model)

    assert len(tasks) == 2
    assert tasks[0].kind == tasks[1].kind == "code"
    assert tasks[0].plan_id == tasks[1].plan_id
    assert tasks[0].depends_on is None
    assert tasks[1].depends_on == tasks[0].id
    assert queue.claim_next().id == tasks[0].id
    queue.complete(tasks[0].id, "step one complete")
    assert queue.claim_next().id == tasks[1].id


def test_failed_plan_step_blocks_downstream_steps(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=1)
    tasks = queue.enqueue_plan("Implement feature", tmp_path, ["Core", "Tests"])
    claimed = queue.claim_next()
    queue.fail(claimed.id, "could not implement", retry=True)

    assert queue.claim_next() is None
    current = {task.id: task for task in queue.list_tasks()}
    assert current[tasks[1].id].status == "failed"
    assert "prerequisite" in current[tasks[1].id].error


def test_run_history_survives_queue_reload(tmp_path):
    path = tmp_path / "tasks.json"
    queue = TaskQueue(path)
    queued = queue.enqueue("Inspect", tmp_path)
    claimed = queue.claim_next()
    queue.complete(claimed.id, "done")

    restored = TaskQueue(path).list_tasks()[0]
    assert restored.id == queued.id
    assert [event["action"] for event in restored.history] == ["queued", "started", "succeeded"]


def test_planner_rejects_unbounded_or_malformed_plan(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    model = FakePlanner(json.dumps({"steps": ["only one"]}))
    with pytest.raises(QueueError, match="between 2 and 8"):
        planner.plan_objective(queue, str(tmp_path), "Do work", model)
    assert queue.list_tasks() == []


def test_supervisor_processes_one_task_and_stops_at_limit(tmp_path, monkeypatch):
    queue = TaskQueue(tmp_path / "tasks.json")
    task = queue.enqueue("Read only task", tmp_path, kind="diagnose")

    def fake_diagnostic(queue_arg, model_client=None, *, claimed_task=None):
        assert claimed_task.id == task.id
        return queue_arg.complete(claimed_task.id, "report")

    monkeypatch.setattr(supervisor, "run_next_diagnostic", fake_diagnostic)
    count = supervisor.run_supervisor(queue, poll_interval=0.1, max_tasks=1, recover=False)

    assert count == 1
    assert queue.list_tasks()[0].status == "succeeded"


def test_supervisor_returns_immediately_when_stop_already_requested(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    stop = threading.Event()
    stop.set()

    assert supervisor.run_supervisor(queue, stop_event=stop, recover=False) == 0


def test_plan_steps_are_bounded(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    with pytest.raises(QueueError, match="between 1 and 8"):
        queue.enqueue_plan("Objective", tmp_path, ["one"] * 9)

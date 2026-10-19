from types import SimpleNamespace

import agent
from task_queue import TaskQueue


def test_task_add_can_queue_coding_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(tmp_path / "tasks.json"))
    assert agent.main(["task", "add", "--workspace", str(tmp_path), "--mode", "code", "Fix", "tests"]) == 0
    task = TaskQueue(tmp_path / "tasks.json").list_tasks()[0]
    assert task.kind == "code"
    assert "Queued task" in capsys.readouterr().out


def test_task_plan_reports_created_steps(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(tmp_path / "tasks.json"))
    created = [
        SimpleNamespace(plan_id="plan-1", step_index=1, id="task-1", prompt="Objective\nPlan step 1/2: Implement core"),
        SimpleNamespace(plan_id="plan-1", step_index=2, id="task-2", prompt="Objective\nPlan step 2/2: Add tests"),
    ]
    monkeypatch.setattr(
        agent, "plan_objective",
        lambda queue, workspace, objective, publish=False, allowed_paths=None, adaptive=False: created,
    )
    assert agent.main(["task", "plan", "--workspace", str(tmp_path), "Implement", "feature"]) == 0
    output = capsys.readouterr().out
    assert "2 dependent coding steps" in output
    assert "Implement core" in output



def test_runtime_controls_pause_resume_cancel_and_status(tmp_path, monkeypatch, capsys):
    queue_path = tmp_path / "tasks.json"
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(queue_path))
    queue = TaskQueue(queue_path)
    first = queue.enqueue("First pending task", tmp_path)
    second = queue.enqueue("Second pending task", tmp_path)

    assert agent.main(["task", "pause"]) == 0
    assert queue.claim_next() is None
    assert queue.runtime_control()["paused"] is True

    assert agent.main(["task", "resume"]) == 0
    claimed = queue.claim_next()
    assert claimed.id == first.id

    assert agent.main(["task", "cancel"]) == 0
    tasks = {task.id: task for task in queue.list_tasks()}
    assert tasks[first.id].status == "running"
    assert tasks[second.id].status == "cancelled"
    assert queue.claim_next() is None
    assert queue.runtime_control()["cancel_requested"] is True

    assert agent.main(["task", "status"]) == 0
    output = capsys.readouterr().out
    assert "cancel requested" in output
    assert "running=1" in output
    assert "cancelled=1" in output

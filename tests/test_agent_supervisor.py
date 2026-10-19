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
        lambda queue, workspace, objective, publish=False, allowed_paths=None: created,
    )
    assert agent.main(["task", "plan", "--workspace", str(tmp_path), "Implement", "feature"]) == 0
    output = capsys.readouterr().out
    assert "2 dependent coding steps" in output
    assert "Implement core" in output

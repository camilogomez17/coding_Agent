from pathlib import Path
from types import SimpleNamespace

import agent
from model_client import ModelError
from task_queue import TaskQueue


def test_task_add_and_list_use_persistent_queue(tmp_path, monkeypatch, capsys):
    queue_path = tmp_path / "state" / "tasks.json"
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(queue_path))

    result = agent.main(
        ["task", "add", "--workspace", str(tmp_path), "Review", "the", "project"]
    )
    add_output = capsys.readouterr().out

    assert result == 0
    assert "Queued task" in add_output
    assert queue_path.exists()

    result = agent.main(["task", "list", "--status", "pending"])
    list_output = capsys.readouterr().out

    assert result == 0
    assert "pending" in list_output
    assert "Review the project" in list_output


def test_task_add_rejects_missing_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(tmp_path / "tasks.json"))

    result = agent.main(
        ["task", "add", "--workspace", str(tmp_path / "missing"), "Do", "work"]
    )
    output = capsys.readouterr()

    assert result == 1
    assert "not an existing directory" in output.err


def test_ask_uses_model_client(monkeypatch, capsys):
    seen = {}

    class FakeModelClient:
        def generate(self, prompt, system=None):
            seen["prompt"] = prompt
            seen["system"] = system
            return "model response"

    monkeypatch.setattr(agent, "ModelClient", FakeModelClient)

    result = agent.main(["ask", "--system", "Be concise", "Explain", "this"])

    assert result == 0
    assert seen == {"prompt": "Explain this", "system": "Be concise"}
    assert capsys.readouterr().out.strip() == "model response"


def test_ask_reports_model_errors_without_traceback(monkeypatch, capsys):
    class BrokenModelClient:
        def generate(self, prompt, system=None):
            raise ModelError("model unavailable")

    monkeypatch.setattr(agent, "ModelClient", BrokenModelClient)

    result = agent.main(["ask", "Explain", "this"])
    output = capsys.readouterr()

    assert result == 1
    assert "Error: model unavailable" in output.err


def test_inspect_command_returns_inspector_exit_code(monkeypatch, tmp_path):
    seen = {}

    def fake_inspect(workspace):
        seen["workspace"] = workspace
        return 1

    monkeypatch.setattr(agent, "inspect_repository", fake_inspect)

    result = agent.main(["inspect", str(tmp_path)])

    assert result == 1
    assert seen["workspace"] == str(tmp_path)


def test_task_diagnose_prints_persisted_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(tmp_path / "tasks.json"))
    completed = SimpleNamespace(
        id="diagnostic-123",
        status="succeeded",
        result="Found a possible issue in app.py.",
        error=None,
    )
    seen = {}

    def fake_diagnose(queue):
        seen["queue"] = queue
        return completed

    monkeypatch.setattr(agent, "run_next_diagnostic", fake_diagnose)

    result = agent.main(["task", "diagnose"])
    output = capsys.readouterr().out

    assert result == 0
    assert seen["queue"].__class__ is TaskQueue
    assert "Found a possible issue in app.py." in output


def test_task_show_prints_saved_result(tmp_path, monkeypatch, capsys):
    queue_path = tmp_path / "tasks.json"
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(queue_path))
    queue = TaskQueue(queue_path)
    task = queue.enqueue("Explain project", tmp_path)
    queue.claim_next()
    queue.complete(task.id, result="Read-only report.")

    result = agent.main(["task", "show", task.id])
    output = capsys.readouterr().out

    assert result == 0
    assert "Status: succeeded" in output
    assert "Read-only report." in output


def test_task_show_reports_unknown_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(tmp_path / "tasks.json"))

    result = agent.main(["task", "show", "unknown-id"])
    output = capsys.readouterr()

    assert result == 1
    assert "Task not found: unknown-id" in output.err



def test_task_code_runs_one_queued_coding_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(tmp_path / "tasks.json"))
    completed = SimpleNamespace(
        id="coding-123",
        status="succeeded",
        result="Branch: agent/task-example",
        error=None,
    )
    seen = {}

    def fake_code(queue):
        seen["queue"] = queue
        return completed

    monkeypatch.setattr(agent, "run_next_coding_task", fake_code)
    result = agent.main(["task", "code"])
    output = capsys.readouterr().out

    assert result == 0
    assert seen["queue"].__class__ is TaskQueue
    assert "Coding report" in output
    assert "agent/task-example" in output

def test_task_add_persists_explicit_allowed_paths(tmp_path, monkeypatch, capsys):
    queue_path = tmp_path / "tasks.json"
    monkeypatch.setenv("AGENT_QUEUE_PATH", str(queue_path))

    result = agent.main([
        "task", "add", "--workspace", str(tmp_path), "--mode", "code",
        "--allow-path", "coding_engine.py",
        "--allow-path", "tests/test_coding_engine.py",
        "Improve", "failure", "recovery",
    ])

    assert result == 0
    assert "Queued task" in capsys.readouterr().out
    task = TaskQueue(queue_path).list_tasks()[0]
    assert task.allowed_paths == ["coding_engine.py", "tests/test_coding_engine.py"]


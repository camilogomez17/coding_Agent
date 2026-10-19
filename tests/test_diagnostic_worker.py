from diagnostic_worker import DIAGNOSTIC_SYSTEM_PROMPT, run_next_diagnostic
from task_queue import TaskQueue


class FakeModelClient:
    def __init__(self, result="Likely issue in main.py", error=None):
        self.result = result
        self.error = error
        self.calls = []

    def generate(self, prompt, system=None):
        self.calls.append((prompt, system))
        if self.error:
            raise self.error
        return self.result


def make_workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "main.py").write_text(
        "def answer():\n    return 42\n",
        encoding="utf-8",
    )
    return workspace


def test_diagnostic_claims_one_task_and_persists_report(tmp_path):
    workspace = make_workspace(tmp_path)
    queue = TaskQueue(tmp_path / "state" / "tasks.json")
    task = queue.enqueue("Find likely bugs", workspace)
    client = FakeModelClient("No obvious bugs found.")

    result = run_next_diagnostic(queue, client)

    assert result.status == "succeeded"
    assert result.id == task.id
    assert result.result == "No obvious bugs found."
    assert len(client.calls) == 1
    prompt, system = client.calls[0]
    assert "Find likely bugs" in prompt
    assert "def answer()" in prompt
    assert "READ-ONLY mode" in system
    assert queue.list_tasks(status="succeeded")[0].result == "No obvious bugs found."


def test_diagnostic_does_not_change_workspace_files(tmp_path):
    workspace = make_workspace(tmp_path)
    source = workspace / "main.py"
    before = source.read_bytes()
    queue = TaskQueue(tmp_path / "state" / "tasks.json")
    queue.enqueue("Explain this project", workspace)

    run_next_diagnostic(queue, FakeModelClient("Analysis only."))

    assert source.read_bytes() == before
    assert sorted(path.name for path in workspace.iterdir()) == ["main.py"]


def test_diagnostic_returns_none_when_queue_is_empty(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.json")
    client = FakeModelClient()

    assert run_next_diagnostic(queue, client) is None
    assert client.calls == []


def test_model_failure_is_recorded_and_task_is_retried(tmp_path):
    workspace = make_workspace(tmp_path)
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=2)
    queue.enqueue("Find bugs", workspace)

    result = run_next_diagnostic(queue, FakeModelClient(error=RuntimeError("model offline")))

    assert result.status == "pending"
    assert result.attempts == 1
    assert "RuntimeError: model offline" in result.error


def test_missing_workspace_is_recorded_as_task_failure(tmp_path):
    workspace = make_workspace(tmp_path)
    queue = TaskQueue(tmp_path / "tasks.json", max_attempts=1)
    queue.enqueue("Diagnose missing project", workspace)
    (workspace / "main.py").unlink()
    workspace.rmdir()

    result = run_next_diagnostic(queue, FakeModelClient())

    assert result.status == "failed"
    assert "ProjectContextError" in result.error


def test_diagnostic_prompt_treats_repository_content_as_untrusted():
    assert "untrusted data" in DIAGNOSTIC_SYSTEM_PROMPT
    assert "READ-ONLY" in DIAGNOSTIC_SYSTEM_PROMPT

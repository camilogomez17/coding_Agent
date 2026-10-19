import json
import subprocess

import pytest

from coding_engine import (
    CodingEngineError,
    _parse_proposal,
    _validate_changes,
    ProposedChange,
    run_coding_task,
)


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, prompt, system=None):
        self.prompts.append((prompt, system))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def make_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Coding Agent Tests")
    git(root, "config", "user.email", "coding-agent-tests@example.invalid")
    (root / "app.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (root / "test_app.py").write_text(
        "from app import answer\n\ndef test_answer():\n    assert answer() == 1\n",
        encoding="utf-8",
    )
    git(root, "add", "app.py", "test_app.py")
    git(root, "commit", "-m", "initial")
    return root


def proposal(path, content, summary="Fix task"):
    return json.dumps({
        "summary": summary,
        "changes": [{"path": path, "content": content, "reason": "test proposal"}],
    })


def test_parse_proposal_rejects_traversal_and_duplicate_paths():
    with pytest.raises(CodingEngineError, match="Unsafe relative path"):
        _parse_proposal(proposal("../outside.py", "x = 1\n"))
    duplicate = json.dumps({"changes": [
        {"path": "app.py", "content": "x=1"},
        {"path": "app.py", "content": "x=2"},
    ]})
    with pytest.raises(CodingEngineError, match="Duplicate path"):
        _parse_proposal(duplicate)


def test_parse_proposal_accepts_json_code_fence():
    fenced = " ```json\n" + proposal("app.py", "x = 1\n") + "\n``` "
    summary, changes = _parse_proposal(fenced)

    assert summary == "Fix task"
    assert len(changes) == 1
    assert changes[0].path == "app.py"
    assert changes[0].content == "x = 1\n"


def test_validator_blocks_secrets_workflows_and_unapproved_extensions(tmp_path):
    root = tmp_path.resolve()
    for path in [".env", ".github/workflows/test.yml", "private/token.py", "payload.sh"]:
        with pytest.raises(CodingEngineError):
            _validate_changes(root, [ProposedChange(path, "content")])


def test_engine_commits_verified_changes_on_new_branch(tmp_path):
    root = make_repo(tmp_path)
    model = FakeModel(proposal("app.py", "def answer():\n    return 2\n", "Change answer"),
                      proposal("app.py", "def answer():\n    \"\"\"Return the expected answer.\"\"\"\n    return 1\n", "Retry with passing implementation"))
    # The first proposal fails tests, is rolled back, and the retry passes.
    result = run_coding_task(root, "Make answer return 2", model, max_attempts=2)

    assert result.branch.startswith("agent/task-make-answer-return-2-")
    assert result.branch != "main"
    assert result.attempts == 2
    assert result.changed_files == ("app.py",)
    assert git(root, "branch", "--show-current").stdout.strip() == result.branch
    assert git(root, "rev-parse", "HEAD").stdout.strip() == result.commit
    assert "pytest passed" in result.test_output or "1 passed" in result.test_output
    assert git(root, "status", "--porcelain").stdout.strip() == ""


def test_engine_rejects_dirty_workspace_without_creating_branch(tmp_path):
    root = make_repo(tmp_path)
    (root / "app.py").write_text("dirty = True\n", encoding="utf-8")
    model = FakeModel(proposal("app.py", "clean = True\n"))

    with pytest.raises(CodingEngineError, match="clean Git working tree"):
        run_coding_task(root, "Change app", model)

    assert git(root, "branch", "--show-current").stdout.strip() == "main"
    assert not model.prompts


def test_engine_rolls_back_when_all_attempts_fail(tmp_path):
    root = make_repo(tmp_path)
    before = (root / "app.py").read_text(encoding="utf-8")
    model = FakeModel(
        proposal("app.py", "def answer(:\n    return 9\n"),
        proposal("app.py", "def answer():\n    return 9\n"),
    )

    with pytest.raises(CodingEngineError, match="returned to 'main'"):
        run_coding_task(root, "Break answer", model, max_attempts=2)

    assert (root / "app.py").read_text(encoding="utf-8") == before
    assert git(root, "status", "--porcelain").stdout.strip() == ""
    assert git(root, "branch", "--show-current").stdout.strip() == "main"
    assert git(root, "branch", "--list", "agent/task-break-answer-*").stdout.strip() == ""


def test_engine_refuses_more_than_three_attempts(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(CodingEngineError, match="between 1 and 3"):
        run_coding_task(root, "do work", FakeModel(), max_attempts=4)

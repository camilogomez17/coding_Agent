import subprocess

import git_workflow


def make_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Agent Tests"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "agent-tests@example.invalid"], cwd=root, check=True)
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/repo.git"],
        cwd=root, check=True,
    )
    subprocess.run(["git", "switch", "-c", "agent/task-test"], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("verified\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "verified change"], cwd=root, check=True, capture_output=True)
    return root


def test_publish_pushes_task_branch_and_creates_pr(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    calls = []
    original_run = git_workflow._run

    def fake_run(args, *, cwd, timeout=30, check=True):
        calls.append(args)
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 1, "", "no pull request")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo/pull/12\n", "")
        return original_run(args, cwd=cwd, timeout=timeout, check=check)

    monkeypatch.setattr(git_workflow, "_run", fake_run)
    url = git_workflow.publish_branch(
        root,
        branch="agent/task-test",
        base_branch="main",
        title="Agent: Test change",
        summary="Test summary",
        commit="abc123",
        changed_files=("README.md",),
    )

    assert url == "https://github.com/example/repo/pull/12"
    push_call = next(call for call in calls if call[:2] == ["git", "push"])
    assert push_call == ["git", "push", "--set-upstream", "origin", "agent/task-test"]
    create_call = next(call for call in calls if call[:3] == ["gh", "pr", "create"])
    assert "--base" in create_call and create_call[create_call.index("--base") + 1] == "main"
    assert "--head" in create_call and create_call[create_call.index("--head") + 1] == "agent/task-test"


def test_publish_reuses_existing_pr_url(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    original_run = git_workflow._run
    calls = []

    def fake_run(args, *, cwd, timeout=30, check=True):
        calls.append(args)
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo/pull/7\n", "")
        return original_run(args, cwd=cwd, timeout=timeout, check=check)

    monkeypatch.setattr(git_workflow, "_run", fake_run)
    url = git_workflow.publish_branch(
        root,
        branch="agent/task-test",
        base_branch="main",
        title="Agent: Test change",
        summary="Test summary",
        commit="abc123",
        changed_files=("README.md",),
    )

    assert url == "https://github.com/example/repo/pull/7"
    assert not any(call[:3] == ["gh", "pr", "create"] for call in calls)

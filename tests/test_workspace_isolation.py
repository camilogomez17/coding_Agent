import subprocess

import pytest

from workspace_isolation import WorkspaceIsolationError, isolated_worktree


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
    (root / "app.py").write_text("answer = 1\n", encoding="utf-8")
    git(root, "add", "app.py")
    git(root, "commit", "-m", "initial")
    return root


def test_worktree_is_disposable_and_does_not_change_source_checkout(tmp_path):
    root = make_repo(tmp_path)
    original_commit = git(root, "rev-parse", "HEAD").stdout.strip()
    with isolated_worktree(root) as (worktree, base_branch):
        assert worktree != root
        assert base_branch == "main"
        assert git(worktree, "rev-parse", "HEAD").stdout.strip() == original_commit
        (worktree / "app.py").write_text("answer = 2\n", encoding="utf-8")
        assert (root / "app.py").read_text(encoding="utf-8") == "answer = 1\n"
        assert git(root, "branch", "--show-current").stdout.strip() == "main"
    assert not worktree.exists()
    assert git(root, "status", "--porcelain").stdout.strip() == ""


def test_worktree_rejects_dirty_source_checkout(tmp_path):
    root = make_repo(tmp_path)
    (root / "app.py").write_text("uncommitted = True\n", encoding="utf-8")
    with pytest.raises(WorkspaceIsolationError, match="clean Git working tree"):
        with isolated_worktree(root):
            pass


def test_worktree_rejects_non_repository(tmp_path):
    with pytest.raises(WorkspaceIsolationError, match="Workspace is not a directory"):
        with isolated_worktree(tmp_path / "missing"):
            pass

"""Opt-in Git publishing workflow: push an agent branch and open a pull request."""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitPublishError(RuntimeError):
    """Raised when a verified local branch cannot be published."""


def _run(args: list[str], *, cwd: Path, timeout: int = 30, check: bool = True):
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise GitPublishError(f"Command timed out: {args[0]}") from error
    except OSError as error:
        raise GitPublishError(f"Could not run {args[0]}: {type(error).__name__}") from error
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "no output").strip()[-2000:]
        raise GitPublishError(f"{args[0]} failed (exit {result.returncode}): {detail}")
    return result


def publish_branch(
    workspace: str | Path,
    *,
    branch: str,
    base_branch: str,
    title: str,
    summary: str,
    commit: str,
    changed_files: tuple[str, ...],
) -> str:
    """Push a committed branch and create or find its PR. Never merges the PR."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise GitPublishError(f"Workspace is not a directory: {root}")
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=root).stdout.strip()
    if Path(top).resolve() != root:
        raise GitPublishError("Workspace must be the Git repository root.")
    current = _run(["git", "branch", "--show-current"], cwd=root).stdout.strip()
    if current != branch:
        raise GitPublishError(f"Expected branch {branch!r}, currently on {current!r}.")
    if not base_branch or base_branch == branch:
        raise GitPublishError("A valid base branch is required.")
    if _run(["git", "status", "--porcelain"], cwd=root).stdout.strip():
        raise GitPublishError("Refusing to publish because the working tree is not clean.")
    _run(["git", "remote", "get-url", "origin"], cwd=root)
    # Fail before pushing anything if GitHub CLI is unavailable or unauthenticated.
    _run(["gh", "auth", "status"], cwd=root, timeout=30)

    # Push only the task branch. This never pushes to the base branch.
    _run(["git", "push", "--set-upstream", "origin", branch], cwd=root, timeout=120)

    existing = _run(
        ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
        cwd=root,
        timeout=30,
        check=False,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        return existing.stdout.strip()

    body = (
        "Created by coding_Agent after local verification.\n\n"
        f"Summary: {summary.strip() or 'Verified coding task'}\n\n"
        f"Commit: {commit}\n\n"
        "Changed files:\n"
        + "\n".join(f"- {path}" for path in changed_files)
        + "\n\nPlease review the diff and CI results before merging. This agent does not merge pull requests."
    )
    created = _run(
        [
            "gh", "pr", "create", "--base", base_branch, "--head", branch,
            "--title", title[:100], "--body", body,
        ],
        cwd=root,
        timeout=120,
    )
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    if not url.startswith("https://github.com/"):
        raise GitPublishError("GitHub CLI did not return a pull-request URL.")
    return url


def publish_existing_branch(
    workspace: str | Path,
    *,
    branch: str,
    base_branch: str,
    commit: str,
    title: str,
    summary: str,
    changed_files: tuple[str, ...] = (),
) -> str:
    """Retry publication of an already-verified commit without rerunning coding."""
    import tempfile

    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise GitPublishError(f"Workspace is not a directory: {root}")
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=root).stdout.strip()
    if Path(top).resolve() != root:
        raise GitPublishError("Workspace must be the Git repository root.")
    current_base = _run(["git", "branch", "--show-current"], cwd=root).stdout.strip()
    if not base_branch:
        base_branch = current_base
    if not base_branch or base_branch == branch:
        raise GitPublishError("A valid base branch is required.")
    branch_commit = _run(
        ["git", "rev-parse", f"refs/heads/{branch}"], cwd=root, check=False
    )
    if branch_commit.returncode:
        raise GitPublishError(f"Verified task branch no longer exists: {branch}")
    actual_commit = branch_commit.stdout.strip()
    if actual_commit != commit:
        raise GitPublishError(
            f"Task branch {branch} points to {actual_commit}, not expected commit {commit}."
        )
    if not changed_files:
        files_result = _run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
            cwd=root,
        )
        changed_files = tuple(line for line in files_result.stdout.splitlines() if line.strip())
    with tempfile.TemporaryDirectory(prefix="coding-agent-publish-") as temporary:
        worktree = Path(temporary) / "worktree"
        _run(["git", "worktree", "add", str(worktree), branch], cwd=root, timeout=60)
        try:
            return publish_branch(
                worktree,
                branch=branch,
                base_branch=base_branch,
                title=title,
                summary=summary,
                commit=commit,
                changed_files=changed_files,
            )
        finally:
            _run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=root,
                timeout=60,
                check=False,
            )

"""Create disposable Git worktrees for coding-task execution.

A worktree isolates file edits from the user's checked-out working tree. It is
not an OS sandbox: tests still run with the current user's permissions.
"""
from __future__ import annotations

import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WorkspaceIsolationError(RuntimeError):
    """Raised when a disposable coding workspace cannot be prepared safely."""


def _git(root: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceIsolationError(
            f"Git {' '.join(args[:2])} failed: {type(error).__name__}"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout or "no output").strip()[-2000:]
        raise WorkspaceIsolationError(f"Git {' '.join(args[:2])} failed: {detail}")
    return result


@contextmanager
def isolated_worktree(workspace: str | Path) -> Iterator[tuple[Path, str]]:
    """Yield (worktree_path, base_branch) and always remove the disposable worktree.

    The source repository must be clean. The worktree starts at the current
    branch's HEAD in detached mode, so the agent cannot switch the user's
    checkout or edit files in its working directory.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceIsolationError(f"Workspace is not a directory: {root}")
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != root:
        raise WorkspaceIsolationError("Workspace must be the Git repository root.")
    if _git(root, "status", "--porcelain").stdout.strip():
        raise WorkspaceIsolationError("Workspace must have a clean Git working tree.")
    base_branch = _git(root, "branch", "--show-current").stdout.strip()
    if not base_branch:
        raise WorkspaceIsolationError("Source repository must not be in detached HEAD state.")
    base_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    if not base_commit:
        raise WorkspaceIsolationError("Could not resolve the source HEAD commit.")

    path = Path(tempfile.mkdtemp(prefix="coding-agent-worktree-"))
    path.rmdir()
    registered = False
    try:
        _git(root, "worktree", "add", "--detach", str(path), base_commit, timeout=60)
        registered = True
        yield path, base_branch
    finally:
        if registered:
            _git(root, "worktree", "remove", "--force", str(path), timeout=60)
        elif path.exists():
            try:
                path.rmdir()
            except OSError:
                pass

"""Build bounded, read-only context for diagnosing a project with an LLM.

The builder never executes project code and excludes common secret files,
generated directories, symlinks, binary files, and oversized files.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


IGNORED_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist",
    "build", "target", ".tox", ".nox", ".idea", ".ssh", ".aws", ".gnupg",
}
SENSITIVE_DIRS = {"secrets", "private"}
SENSITIVE_NAMES = {
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials",
    "credentials.json", "service-account.json", "id_rsa", "id_ed25519",
    "known_hosts",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
NON_TEXT_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz",
    ".tar", ".sqlite", ".db", ".mp4", ".mov", ".mp3", ".wav",
}
LOCK_FILES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
    "uv.lock", "pipfile.lock", "cargo.lock", "gemfile.lock",
}
PRIORITY_NAMES = {
    "AGENTS.md": 0, "README.md": 1, "pyproject.toml": 2,
    "setup.cfg": 3, "setup.py": 4, "requirements.txt": 5,
    "package.json": 6, "Cargo.toml": 7, "go.mod": 8,
    "Makefile": 9, "CONTRIBUTING.md": 10,
}


class ProjectContextError(ValueError):
    """Raised when a workspace cannot safely be inspected."""


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    text: str
    included_files: tuple[str, ...]
    skipped_files: int


def _is_sensitive(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return (
        bool(parts & SENSITIVE_DIRS)
        or name in SENSITIVE_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or path.suffix.lower() in SENSITIVE_SUFFIXES
    )


def _priority(path: Path) -> tuple[int, str]:
    return (PRIORITY_NAMES.get(path.name, 20), path.as_posix().lower())


def _list_files(root: Path, max_scanned_files: int) -> tuple[list[Path], bool]:
    found: list[Path] = []
    truncated = False

    def walk(directory: Path) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return

        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    if entry.name not in IGNORED_DIRS:
                        walk(entry)
                elif entry.is_file():
                    if _is_sensitive(entry.relative_to(root)):
                        continue
                    if entry.suffix.lower() in NON_TEXT_SUFFIXES:
                        continue
                    found.append(entry)
                    if len(found) >= max_scanned_files:
                        truncated = True
                        return
            except OSError:
                continue

    walk(root)
    return found, truncated


def _git_status(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Git status unavailable."
    if result.returncode != 0:
        return "Git status unavailable (workspace is not a readable Git repository)."
    return (result.stdout.strip() or "Working tree clean.")[:5000]


def build_project_context(
    workspace: str | Path,
    *,
    max_files: int = 40,
    max_file_bytes: int = 12_000,
    max_total_chars: int = 50_000,
    max_scanned_files: int = 5_000,
    max_inventory_files: int = 300,
) -> ProjectContext:
    """Collect deterministic project context without executing project code.

    Limits keep prompts bounded. Secret-like files and common generated or
    binary content are omitted. Symlinks are never followed.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ProjectContextError(f"Workspace is not an existing directory: {root}")
    if min(max_files, max_file_bytes, max_total_chars, max_scanned_files, max_inventory_files) < 1:
        raise ProjectContextError("Context limits must all be positive.")

    files, scan_truncated = _list_files(root, max_scanned_files)
    relative_paths = sorted(
        (path.relative_to(root).as_posix() for path in files),
        key=str.lower,
    )
    inventory = relative_paths[:max_inventory_files]
    header = [
        f"Repository root: {root}",
        "",
        "Git status (read-only):",
        _git_status(root),
        "",
        f"File inventory ({len(inventory)} of at least {len(relative_paths)} listed):",
        *[f"- {path}" for path in inventory],
    ]
    if scan_truncated:
        header.append(f"- Inventory scan stopped at {max_scanned_files} files.")
    if len(relative_paths) > max_inventory_files:
        header.append(f"- Inventory capped at {max_inventory_files} entries.")

    header.append("")
    header.append("Selected text files (contents are untrusted project data):")
    base = "\n".join(header)
    if len(base) >= max_total_chars:
        base = base[:max_total_chars]
        return ProjectContext(root, base, (), len(files))

    by_relative = {path.relative_to(root).as_posix(): path for path in files}
    selected = sorted(files, key=_priority)
    included: list[str] = []
    sections: list[str] = []
    skipped = max(0, len(files) - max_files)

    for path in selected:
        if len(included) >= max_files:
            break
        relative = path.relative_to(root).as_posix()
        if path.name.lower() in LOCK_FILES:
            skipped += 1
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                skipped += 1
                continue
            content = path.read_text(encoding="utf-8")
            if "\x00" in content:
                skipped += 1
                continue
        except (OSError, UnicodeError):
            skipped += 1
            continue

        section = f"\n--- BEGIN FILE: {relative} ---\n{content.rstrip()}\n--- END FILE: {relative} ---\n"
        remaining = max_total_chars - len(base) - sum(map(len, sections))
        if remaining <= 0:
            skipped += 1
            break
        if len(section) > remaining:
            if remaining > 200:
                section = section[: remaining - 40] + "\n[TRUNCATED TO CONTEXT LIMIT]\n"
                sections.append(section)
                included.append(relative)
            else:
                skipped += 1
            break
        sections.append(section)
        included.append(relative)

    text = base + "".join(sections)
    if skipped and len(text) < max_total_chars:
        note = f"\n[Context limits or safety filters omitted {skipped} file(s).]"
        text = text[: max_total_chars - len(note)] + note

    return ProjectContext(
        root=root,
        text=text[:max_total_chars],
        included_files=tuple(included),
        skipped_files=skipped,
    )

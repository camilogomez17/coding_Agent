from pathlib import Path
import ast
import subprocess
import sys


IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".next",
}


def find_files(repo: Path, suffix: str) -> list[Path]:
    """Find files with the requested suffix, skipping generated directories."""
    found = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            # Unreadable directories should not stop the whole scan.
            return

        for entry in entries:
            try:
                # Do not follow symlinks: they can lead outside the repository
                # or create directory loops.
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in IGNORED_DIRS:
                        walk(entry)
                elif entry.suffix == suffix:
                    found.append(entry)
            except OSError:
                # A file may disappear or become inaccessible during a scan.
                continue

    walk(repo)
    return found


def inspect_repository(root: str) -> int:
    """Inspect a repository and return 0 on success, 1 when checks fail."""
    repo = Path(root).resolve()

    if not repo.is_dir():
        print(f"Error: directory not found: {repo}")
        return 1

    print(f"Repository: {repo}")

    python_files = find_files(repo, ".py")
    print(f"\nPython files found: {len(python_files)}")

    # Parse source without executing it.
    syntax_errors = []

    for file in python_files:
        try:
            ast.parse(file.read_text(encoding="utf-8"))
        except SyntaxError as error:
            syntax_errors.append(
                f"{file.relative_to(repo)}:{error.lineno}: {error.msg}"
            )
        except (OSError, UnicodeError) as error:
            syntax_errors.append(
                f"{file.relative_to(repo)}: Could not read file: {error}"
            )

    print("\nSyntax check:")
    if syntax_errors:
        for error in syntax_errors:
            print(f"FAIL: {error}")
    else:
        print("PASS: No Python syntax errors found.")

    exit_code = 1 if syntax_errors else 0

    # Only run pytest when a test suite appears to exist.
    test_files = [
        file
        for file in python_files
        if file.name.startswith("test_") or file.name.endswith("_test.py")
    ]

    print("\nTest suite:")
    if not test_files:
        print("SKIP: No test files detected.")
    else:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("FAIL: pytest timed out after 120 seconds.")
            return 1
        except OSError as error:
            print(f"FAIL: Could not start pytest: {error}")
            return 1

        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")

        print(f"Pytest exit code: {result.returncode}")
        if result.returncode != 0:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    raise SystemExit(inspect_repository(target))

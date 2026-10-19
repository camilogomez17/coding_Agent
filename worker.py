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
    found = []

    def walk(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
        except PermissionError:
            return

        for entry in entries:
            if entry.is_dir():
                if entry.name not in IGNORED_DIRS:
                    walk(entry)
            elif entry.suffix == suffix:
                found.append(entry)

    walk(repo)
    return sorted(found)


def inspect_repository(root: str) -> None:
    repo = Path(root).resolve()

    if not repo.is_dir():
        print(f"Error: directory not found: {repo}")
        return

    print(f"Repository: {repo}")

    python_files = find_files(repo, ".py")
    print(f"\nPython files found: {len(python_files)}")

    # Check Python syntax without executing the files.
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
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
        )

        print(result.stdout)
        if result.stderr:
            print(result.stderr)

        print(f"Pytest exit code: {result.returncode}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    inspect_repository(target)

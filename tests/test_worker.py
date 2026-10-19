from worker import find_files, inspect_repository


def test_inspector_accepts_valid_python_file(tmp_path, capsys):
    (tmp_path / "main.py").write_text(
        "print('hello')\n",
        encoding="utf-8",
    )

    inspect_repository(str(tmp_path))
    output = capsys.readouterr().out

    assert "Python files found: 1" in output
    assert "PASS: No Python syntax errors found." in output
    assert "SKIP: No test files detected." in output


def test_inspector_detects_syntax_error(tmp_path, capsys):
    (tmp_path / "broken.py").write_text(
        "def broken(:\n",
        encoding="utf-8",
    )

    inspect_repository(str(tmp_path))
    output = capsys.readouterr().out

    assert "FAIL: broken.py:1:" in output


def test_inspector_handles_missing_directory(tmp_path, capsys):
    missing_directory = tmp_path / "does_not_exist"

    inspect_repository(str(missing_directory))
    output = capsys.readouterr().out

    assert "Error: directory not found:" in output


def test_find_files_ignores_node_modules(tmp_path):
    (tmp_path / "main.py").write_text(
        "print('hello')\n",
        encoding="utf-8",
    )

    ignored_directory = tmp_path / "node_modules"
    ignored_directory.mkdir()
    (ignored_directory / "hidden.py").write_text(
        "print('hidden')\n",
        encoding="utf-8",
    )

    found_files = find_files(tmp_path, ".py")
    relative_paths = [
        file.relative_to(tmp_path).as_posix()
        for file in found_files
    ]

    assert relative_paths == ["main.py"]

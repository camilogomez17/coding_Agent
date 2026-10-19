import os

import pytest

from project_context import ProjectContextError, build_project_context


def test_context_includes_project_files_and_git_status(tmp_path):
    (tmp_path / "README.md").write_text("# Sample project\nPurpose.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("def answer():\n    return 42\n", encoding="utf-8")

    context = build_project_context(tmp_path)

    assert "README.md" in context.text
    assert "def answer()" in context.text
    assert "Repository root:" in context.text
    assert context.included_files
    assert "main.py" in context.included_files


def test_context_omits_secrets_and_generated_directories(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=secret-value", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"password":"secret-value"}', encoding="utf-8")
    (tmp_path / "safe.py").write_text("print('safe')", encoding="utf-8")
    generated = tmp_path / ".venv"
    generated.mkdir()
    (generated / "hidden.py").write_text("print('hidden-secret')", encoding="utf-8")

    context = build_project_context(tmp_path)

    assert "secret-value" not in context.text
    assert "hidden-secret" not in context.text
    assert "safe.py" in context.text


def test_context_does_not_follow_symlinks(tmp_path):
    outside = tmp_path.parent / "outside-secret.py"
    outside.write_text("outside_secret = True", encoding="utf-8")
    link = tmp_path / "external.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    context = build_project_context(tmp_path)

    assert "outside_secret" not in context.text
    assert "external.py" not in context.included_files


def test_context_enforces_total_character_limit(tmp_path):
    (tmp_path / "large.py").write_text("x = '" + ("a" * 8000) + "'\n", encoding="utf-8")

    context = build_project_context(tmp_path, max_file_bytes=20_000, max_total_chars=1200)

    assert len(context.text) <= 1200


def test_context_rejects_missing_workspace(tmp_path):
    with pytest.raises(ProjectContextError, match="not an existing directory"):
        build_project_context(tmp_path / "missing")


def test_context_rejects_nonpositive_limits(tmp_path):
    with pytest.raises(ProjectContextError, match="must all be positive"):
        build_project_context(tmp_path, max_files=0)


def test_context_skips_binary_and_oversized_files(tmp_path):
    (tmp_path / "binary.py").write_bytes(b"print('a')\x00\x01")
    (tmp_path / "large.py").write_text("x" * 100, encoding="utf-8")
    context = build_project_context(tmp_path, max_file_bytes=20)

    assert "print('a')" not in context.text
    assert "x" * 50 not in context.text
    assert context.skipped_files >= 1

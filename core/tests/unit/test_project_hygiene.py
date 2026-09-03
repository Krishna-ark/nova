"""Repository hygiene checks.

These guard properties that are easy to break by accident and expensive to
notice later: a committed credential, a README that no longer matches the
real setup steps, or a non-ASCII character in Python source.

The last one is not stylistic. A single non-ASCII character in a .py file
previously caused Ruff to crash while rendering a diagnostic, because its
annotation offsets are computed in bytes but bounds-checked in characters.
NOVA is a multilingual project, so Hindi and Hinglish belong in YAML and
JSON data files, never in Python source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nova

REPO_ROOT = Path(nova.__file__).parents[2]


def _python_files() -> list[Path]:
    """Return every Python source file in the project."""
    excluded = {".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".git"}
    return [
        path
        for path in (REPO_ROOT / "core").rglob("*.py")
        if not any(part in excluded for part in path.parts)
    ]


# --- Secrets must never be committed ---


def test_gitignore_excludes_the_env_file() -> None:
    contents = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "\n.env\n" in f"\n{contents}", ".env must be git-ignored"


def test_gitignore_keeps_the_env_template() -> None:
    """The template is committed; the real file is not."""
    contents = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "!.env.example" in contents


def test_env_example_exists() -> None:
    assert (REPO_ROOT / ".env.example").is_file()


def test_env_example_holds_no_credential() -> None:
    """Copying the template unchanged must fail, not yield a weak token.

    An empty value is deliberate. A filler such as "replace-me-with-a-token"
    would be 24 characters and could pass a length check, handing anyone who
    copied the file a working install with a publicly known credential.
    """
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("NOVA_SECURITY__AUTH_TOKEN="):
            assert line.strip() == "NOVA_SECURITY__AUTH_TOKEN=", (
                "the template must not contain a token value"
            )
            return
    pytest.fail("NOVA_SECURITY__AUTH_TOKEN is not documented in .env.example")


def test_gitignore_excludes_database_and_log_artifacts() -> None:
    contents = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in ("*.db", "logs/"):
        assert pattern in contents, f"{pattern} must be git-ignored"


# --- Python source stays ASCII ---


def test_python_sources_are_ascii_only() -> None:
    """Non-ASCII in .py files has crashed Ruff's diagnostic renderer before."""
    offenders: list[str] = []
    for path in _python_files():
        text = path.read_bytes().decode("utf-8")
        bad = {character for character in text if ord(character) > 127}
        if bad:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {sorted(bad)}")

    assert offenders == [], "non-ASCII characters in Python source: " + "; ".join(offenders)


def test_python_sources_end_with_a_newline() -> None:
    missing = [
        str(path.relative_to(REPO_ROOT))
        for path in _python_files()
        if path.read_bytes() and not path.read_bytes().endswith(b"\n")
    ]

    assert missing == [], f"files missing a final newline: {missing}"


def test_python_sources_have_no_byte_order_mark() -> None:
    """A BOM is easy to introduce with a Windows editor and hard to spot."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _python_files()
        if path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]

    assert offenders == [], f"files with a UTF-8 BOM: {offenders}"


# --- Developer tooling is configured ---


def test_pre_commit_config_exists() -> None:
    assert (REPO_ROOT / ".pre-commit-config.yaml").is_file()


@pytest.mark.parametrize("hook", ["ruff-check", "ruff-format", "mypy", "pytest"])
def test_pre_commit_runs_every_quality_gate(hook: str) -> None:
    """A gate that runs in CI but not before a commit gets discovered late."""
    contents = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert f"id: {hook}" in contents


def test_pre_commit_uses_the_local_toolchain() -> None:
    """Pinned copies would drift from the versions the gates actually use."""
    contents = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert "repo: local" in contents
    assert "language: system" in contents


# --- README documents a clean-machine setup ---


def test_readme_exists() -> None:
    assert (REPO_ROOT / "README.md").is_file()


@pytest.mark.parametrize(
    "fragment",
    [
        "py -3.12 -m venv",
        "pip install -e",
        ".env.example",
        "secrets.token_urlsafe",
        "python -m pytest",
        "python -m ruff check",
        "python -m mypy",
        "alembic upgrade head",
    ],
)
def test_readme_documents_the_setup_steps(fragment: str) -> None:
    """Someone with a clean machine must be able to follow the README."""
    contents = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert fragment in contents, f"README does not mention {fragment!r}"


def test_readme_contains_no_credential() -> None:
    """Documentation is a common place for a real token to leak."""
    contents = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    env_file = REPO_ROOT / ".env"

    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("NOVA_SECURITY__AUTH_TOKEN="):
                token = line.split("=", 1)[1].strip()
                if token:
                    assert token not in contents, "the real auth token appears in README.md"

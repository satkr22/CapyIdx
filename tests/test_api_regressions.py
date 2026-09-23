from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import capyidx


def _init_git_repo(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir()
    for relative_path, contents in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CapyIdx API Tests",
            "-c",
            "user.email=capyidx-api-tests@example.invalid",
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "Test fixture",
        ],
        cwd=repo,
        check=True,
    )


def _fixture_files() -> dict[str, str]:
    return {
        "alpha/duplicate.py": (
            "def duplicate():\n"
            "    return \"alpha\"\n"
        ),
        "beta/duplicate.py": (
            "def duplicate():\n"
            "    return \"beta\"\n"
        ),
        "long_module.py": (
            "def long_function(value):\n"
            "    first = value + 1\n"
            "    second = first + 1\n"
            "    third = second + 1\n"
            "    return third\n"
        ),
    }


@pytest.fixture
def indexed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    _init_git_repo(repo, _fixture_files())
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CAPYIDX_HOME", str(tmp_path / "capyidx-home"))
    asyncio.run(capyidx.index_repo(repo, max_chunk_size=128))
    return repo


@pytest.mark.parametrize(
    ("repo_kind", "expected_error"),
    [
        ("missing", FileNotFoundError),
        ("file", NotADirectoryError),
    ],
)
def test_index_repo_rejects_invalid_repository_paths(
    tmp_path: Path,
    repo_kind: str,
    expected_error: type[Exception],
) -> None:
    if repo_kind == "missing":
        repo = tmp_path / "does-not-exist"
    else:
        repo = tmp_path / "not-a-directory"
        repo.write_text("not a repository", encoding="utf-8")

    with pytest.raises(expected_error):
        asyncio.run(capyidx.index_repo(repo))


# def test_index_repo_rejects_directory_without_git(
#     tmp_path: Path,
#     monkeypatch: pytest.MonkeyPatch,
# ) -> None:
#     repo = tmp_path / "not-git"
#     repo.mkdir()
#     monkeypatch.chdir(repo)

#     with pytest.raises(RuntimeError, match="git repository"):
#         asyncio.run(capyidx.index_repo(repo))


def test_unknown_symbol_returns_empty_matches(indexed_repo: Path) -> None:
    result = asyncio.run(capyidx.lookup_symbol(indexed_repo, "does_not_exist"))

    assert result.matches == []
    assert result.selected is None


def test_multiple_matching_symbols_do_not_select_implicitly(indexed_repo: Path) -> None:
    result = asyncio.run(capyidx.lookup_symbol(indexed_repo, "duplicate"))

    assert len(result.matches) == 2
    assert result.selected is None
    assert result.matches[0].path != result.matches[1].path


def test_filter_paths_restricts_matches(indexed_repo: Path) -> None:
    all_matches = asyncio.run(capyidx.lookup_symbol(indexed_repo, "duplicate"))
    selected_path = next(
        match.path for match in all_matches.matches if "/alpha/" in match.path
    )

    result = asyncio.run(
        capyidx.lookup_symbol(
            indexed_repo,
            "duplicate",
            filter_paths=[selected_path],
        )
    )

    assert len(result.matches) == 1
    assert result.matches[0].path == selected_path
    assert result.selected is not None
    assert result.selected.path == selected_path


def test_signature_detail_and_max_lines(indexed_repo: Path) -> None:
    signature = asyncio.run(
        capyidx.lookup_symbol(
            indexed_repo,
            "long_function",
            detail="signature",
            include_children=False,
        )
    )
    truncated = asyncio.run(
        capyidx.lookup_symbol(
            indexed_repo,
            "long_function",
            detail="body",
            include_children=False,
            max_lines=2,
        )
    )

    assert signature.selected is not None
    assert signature.selected.signature == "def long_function(value):"
    assert signature.selected.code == signature.selected.signature
    assert "return" not in signature.selected.code

    assert truncated.selected is not None
    assert "more lines omitted" in truncated.selected.code
    assert "return third" not in truncated.selected.code

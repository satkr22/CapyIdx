from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from contextlib import asynccontextmanager

import pytest

import capyidx
from capyidx.db.schema import init_schema
from capyidx.retrieval.retrieval_pipeline import SymbolLookup
from capyidx.utils.uri1 import get_path_to_uri


@pytest.fixture
def indexed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = {
        "alpha": repo / "alpha" / "duplicate.py",
        "beta": repo / "beta" / "duplicate.py",
        "long": repo / "long_module.py",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    rows = [
        ("duplicate-alpha", "duplicate", paths["alpha"], 'def duplicate():\n    return "alpha"'),
        ("duplicate-beta", "duplicate", paths["beta"], 'def duplicate():\n    return "beta"'),
        ("long-function", "long_function", paths["long"], "def long_function(value):\n    first = value + 1\n    second = first + 1\n    third = second + 1\n    return third"),
    ]
    conn = sqlite3.connect(str(tmp_path / "lookup.sqlite"), check_same_thread=False)
    init_schema(conn)
    tag = f"{get_path_to_uri(str(repo))}::NONE::chunks"
    for symbol_id, name, path, code in rows:
        path_text = get_path_to_uri(str(path))
        signature = code.splitlines()[0]
        conn.execute(
            "INSERT INTO symbols(id,type,name,parentId,startLine,endLine,signature,cacheKey,path) VALUES (?,?,?,?,?,?,?,?,?)",
            (symbol_id, "function", name, None, 1, len(code.splitlines()), signature, symbol_id, path_text),
        )
        chunk_id = f"chunk-{symbol_id}"
        conn.execute(
            "INSERT INTO chunks(id,symbolId,cacheKey,path,idx,pieceIndex,pieceCount,prevChunk,nextChunk,signature,startLine,endLine,content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chunk_id, symbol_id, symbol_id, path_text, 0, 0, 1, None, None, signature, 1, len(code.splitlines()), code),
        )
        conn.execute("INSERT INTO chunk_tags(tag,chunkId) VALUES (?,?)", (tag, chunk_id))
    conn.commit()

    lookup = SymbolLookup(conn, [get_path_to_uri(str(repo))])

    @asynccontextmanager
    async def fake_open_lookup(_repo):
        yield lookup

    monkeypatch.setattr(capyidx.api, "open_lookup", fake_open_lookup)

    async def direct_call(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(capyidx.api.asyncio, "to_thread", direct_call)
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


def test_explicit_symbol_id_selects_one_ambiguous_match(indexed_repo: Path) -> None:
    matches = asyncio.run(capyidx.lookup_symbol(indexed_repo, "duplicate")).matches

    result = asyncio.run(
        capyidx.lookup_symbol(
            indexed_repo,
            "duplicate",
            symbol_id=matches[1].id,
        )
    )

    assert len(result.matches) == 2
    assert result.selected is not None
    assert result.selected.id == matches[1].id


def test_resolve_lookup_reconstructs_all_ambiguous_matches(
    indexed_repo: Path,
) -> None:
    result = asyncio.run(capyidx.resolve_lookup(indexed_repo, "duplicate"))

    assert result.selected is None
    assert [code.id for code in result.codes] == [
        match.id for match in result.matches
    ]
    assert {code.code.rstrip() for code in result.codes} == {
        'def duplicate():\n    return "alpha"',
        'def duplicate():\n    return "beta"',
    }


def test_reconstruct_symbol_supports_signature_detail(
    indexed_repo: Path,
) -> None:
    lookup = asyncio.run(capyidx.lookup_symbol(indexed_repo, "duplicate"))
    symbol_id = lookup.matches[0].id

    reconstructed = asyncio.run(
        capyidx.reconstruct_symbol(
            indexed_repo,
            symbol_id,
            detail="signature",
        )
    )

    assert reconstructed.id == symbol_id
    assert reconstructed.code == "def duplicate():"
    assert "return" not in reconstructed.code

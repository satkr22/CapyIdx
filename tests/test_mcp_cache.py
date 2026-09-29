from __future__ import annotations

from pathlib import Path

import pytest

from capyidx.mcp.cache import SymbolCache, canonical_path
from capyidx.retrieval.models import SymbolCode


def symbol(symbol_id: str, code: str = "x", path: str = "/repo/a.py") -> SymbolCode:
    return SymbolCode(
        id=symbol_id,
        name=symbol_id,
        type="function",
        path=path,
        start_line=1,
        end_line=max(1, len(code.splitlines())),
        code=code,
    )


def test_canonical_path_normalizes_file_uri_and_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = str((tmp_path / "folder/file.py").resolve())

    assert canonical_path("folder/file.py") == expected
    assert canonical_path("file://" + expected) == expected
    assert canonical_path("file:///tmp/a%20b.py") == "/tmp/a b.py"


def test_canonical_path_expands_user_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/test-home")
    assert canonical_path("~/project.py") == "/test-home/project.py"


def test_cache_rejects_negative_capacity() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        SymbolCache(-1)


def test_cache_put_get_promotes_lru_and_tracks_utf8_bytes() -> None:
    cache = SymbolCache(5)
    first = symbol("first", "é")  # two UTF-8 bytes
    second = symbol("second", "abc")

    cache.put(first.id, first.path, first)
    cache.put(second.id, second.path, second)

    assert cache.current_bytes == 5
    assert cache.get(first.id) == first
    # The get(first.id) above promotes first, leaving second as the oldest.
    third = symbol("third", "z")
    cache.put(third.id, third.path, third)
    assert cache.get("second") is None
    assert cache.get("first") == first
    assert cache.get("third") == third


def test_cache_zero_capacity_does_not_retain_entries() -> None:
    cache = SymbolCache(0)
    value = symbol("id", "anything")

    cache.put(value.id, value.path, value)

    assert cache.current_bytes == 0
    assert cache.get(value.id) is None


def test_cache_replacing_id_updates_path_index_and_size() -> None:
    cache = SymbolCache(100)
    old = symbol("same", "old", "/repo/old.py")
    new = symbol("same", "newer", "/repo/new.py")

    cache.put(old.id, old.path, old)
    cache.put(new.id, new.path, new)

    assert cache.get("same") == new
    assert cache.current_bytes == len("newer".encode())
    cache.invalidate_path(old.path)
    assert cache.get("same") == new
    cache.invalidate_path(new.path)
    assert cache.get("same") is None
    assert cache.current_bytes == 0


def test_cache_invalidation_removes_every_symbol_for_uri_or_equivalent_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.py"
    cache = SymbolCache(100)
    one = symbol("one", "1", str(path))
    two = symbol("two", "22", "file://" + str(path))
    other = symbol("other", "333", str(tmp_path / "other.py"))
    for value in (one, two, other):
        cache.put(value.id, value.path, value)

    cache.invalidate_path("file://" + str(path))

    assert cache.get("one") is None
    assert cache.get("two") is None
    assert cache.get("other") == other
    assert cache.current_bytes == 3


def test_cache_clear_removes_entries_and_path_index() -> None:
    cache = SymbolCache(100)
    value = symbol("id", "text")
    cache.put(value.id, value.path, value)

    cache.clear()

    assert cache.current_bytes == 0
    assert cache.get(value.id) is None
    cache.invalidate_path(value.path)  # no-op after clear
    assert cache.current_bytes == 0

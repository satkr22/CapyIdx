from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from capyidx.mcp import handlers
from capyidx.mcp.cache import SymbolCache
from capyidx.retrieval.models import LookupResult, SymbolCode, SymbolMatch
from capyidx.retrieval.retrieval_pipeline import SymbolLookup


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def code(
    symbol_id: str = "id-1",
    text: str = "line 1\nline 2\nline 3\n",
    path: str = "/repo/example.py",
    start: int = 10,
    end: int = 12,
) -> SymbolCode:
    return SymbolCode(
        id=symbol_id,
        name="example",
        type="function",
        path=path,
        start_line=start,
        end_line=end,
        code=text,
    )


def match(symbol_id: str = "id-1", name: str = "example") -> SymbolMatch:
    return SymbolMatch(
        id=symbol_id,
        name=name,
        type="function",
        path="/repo/example.py",
        start_line=10,
        end_line=12,
        signature="def example():",
        match_kind="exact",
    )


class FakeIndexer:
    def __init__(self, ready: bool = True, cache_bytes: int = 10_000) -> None:
        self.system_ready = ready
        self._cache = SymbolCache(cache_bytes)
        self.pending_paths: set[str] = set()


def lookup_object(
    lookup_results: dict[str, LookupResult] | None = None,
    reconstructed: dict[str, Any] | None = None,
) -> SymbolLookup:
    obj = object.__new__(SymbolLookup)
    lookup_results = lookup_results or {}
    reconstructed = reconstructed or {}
    obj.lookup = lambda name: lookup_results[name]  # type: ignore[attr-defined]

    async def reconstruct(symbol_id: str) -> Any:
        value = reconstructed[symbol_id]
        if isinstance(value, BaseException):
            raise value
        return value

    obj.reconstruct = reconstruct  # type: ignore[attr-defined]
    return obj


def test_error_envelope() -> None:
    assert handlers.error("E", "message") == {
        "__xx_e_code": "E",
        "message": "message",
    }


def test_wait_for_path_ready_immediately_succeeds_when_not_pending() -> None:
    indexer = FakeIndexer()
    assert run(handlers.wait_for_path_ready(indexer, "/repo/missing.py")) is True


def test_wait_for_path_ready_times_out_and_validates_arguments() -> None:
    indexer = FakeIndexer()
    indexer.pending_paths.add("/repo/example.py")

    assert run(
        handlers.wait_for_path_ready(indexer, "/repo/example.py", max_wait_s=0)
    ) is False
    with pytest.raises(ValueError, match="max_wait_s"):
        run(handlers.wait_for_path_ready(indexer, "x", max_wait_s=-1))
    with pytest.raises(ValueError, match="poll_s"):
        run(handlers.wait_for_path_ready(indexer, "x", poll_s=0))


def test_wait_for_path_ready_uses_canonical_equivalence_and_stops_when_cleared(
    tmp_path: Path,
) -> None:
    indexer = FakeIndexer()
    path = tmp_path / "example.py"
    indexer.pending_paths.add("file://" + str(path))

    async def scenario() -> bool:
        async def clear() -> None:
            await asyncio.sleep(0)
            indexer.pending_paths.clear()

        task = asyncio.create_task(clear())
        result = await handlers.wait_for_path_ready(
            indexer, str(path), max_wait_s=0.2, poll_s=0.01
        )
        await task
        return result

    assert run(scenario()) is True


def test_load_symbol_returns_cache_hit_without_lookup() -> None:
    indexer = FakeIndexer()
    cached = code()
    indexer._cache.put(cached.id, cached.path, cached)

    class ExplodingLookup:
        def reconstruct(self, _symbol_id: str) -> None:
            raise AssertionError("cache hit should not reconstruct")

    assert run(handlers._load_symbol(indexer, cached.id, ExplodingLookup())) == cached


@pytest.mark.parametrize(
    ("lookup", "expected_code", "expected_message"),
    [
        (None, "LOOKUP_UNAVAILABLE", "symbol lookup is not configured"),
        (SimpleNamespace(reconstruct=lambda _id: (_ for _ in ()).throw(KeyError("x"))), "SYMBOL_NOT_FOUND", "'x'"),
        (SimpleNamespace(reconstruct=lambda _id: "not code"), "LOOKUP_INVALID", "invalid symbol"),
    ],
)
def test_load_symbol_reports_missing_or_invalid_lookup(
    lookup: Any,
    expected_code: str,
    expected_message: str,
) -> None:
    result = run(handlers._load_symbol(FakeIndexer(), "id", lookup))
    assert result["__xx_e_code"] == expected_code
    assert expected_message in result["message"]


def test_load_symbol_supports_sync_and_async_reconstructors() -> None:
    expected = code()
    sync_lookup = SimpleNamespace(reconstruct=lambda _id: expected)

    async def reconstruct(_id: str) -> SymbolCode:
        return expected

    async_lookup = SimpleNamespace(reconstruct=reconstruct)
    assert run(handlers._load_symbol(FakeIndexer(), "id-1", sync_lookup)) == expected
    assert run(handlers._load_symbol(FakeIndexer(), "id-1", async_lookup)) == expected


@pytest.mark.parametrize("limit", [0, -1])
def test_serialize_symbol_rejects_non_positive_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="char_limit"):
        handlers.serialize_symbol_code(code(), char_limit=limit)


def test_serialize_symbol_keeps_short_code_without_truncation() -> None:
    value = handlers.serialize_symbol_code(code(text="short", start=4, end=4), char_limit=20)
    assert value == {
        "symbol_id": "id-1",
        "symbol_name": "example",
        "path": "/repo/example.py",
        "start_line": 4,
        "end_line": 4,
        "code": "short",
    }


def test_serialize_symbol_uses_marker_only_when_limit_is_tiny() -> None:
    value = handlers.serialize_symbol_code(code(), char_limit=3)
    assert value["code"] == "..."
    assert value["truncated"] is True
    assert value["returned_end_line"] == 9
    assert value["total_lines"] ==  3


def test_serialize_symbol_preserves_complete_lines_before_marker() -> None:
    value = handlers.serialize_symbol_code(code(text="line 1\nline 2\nline 3\nline 4\n"), char_limit=25)
    assert value["truncated"] is True
    assert value["code"] == "line 1\n...(truncated)..."
    assert value["returned_end_line"] == 10
    assert value["total_lines"] ==  4


def test_lookup_response_handles_selected_match_and_missing_results() -> None:
    selected = code("selected")
    selected_result = LookupResult(query="one", selected=selected)
    matches_result = LookupResult(query="many", matches=[match("m1"), match("m2")])

    response = handlers._lookup_response([selected_result, matches_result])

    assert response["one"] == [
        {
            "symbol_id": "selected",
            "path": selected.path,
            "start_line": selected.start_line,
            "end_line": selected.end_line,
            "signature": selected.signature,
        }
    ]
    assert [item["symbol_id"] for item in response["many"]] == ["m1", "m2"]
    missing = handlers._lookup_response([LookupResult(query="none")])
    assert missing == [
        {"__xx_e_code": "SYMBOL_NOT_FOUND", "message": "symbol doesn't exist of index. Use `grep`"}
    ]


def test_lookup_response_rejects_non_list_and_preserves_empty_match_lists() -> None:
    invalid = handlers._lookup_response(None)  # type: ignore[arg-type]
    assert invalid[0]["__xx_e_code"] == "LOOKUP_INVALID_RESPONSE"
    assert handlers._lookup_response([]) == {}


def test_warm_cache_caches_selected_and_unchanged_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    indexer = FakeIndexer()
    selected = code("selected")
    reconstructed = code("matched")
    lookup = SimpleNamespace(
        reconstruct=lambda symbol_id: selected if symbol_id == "selected" else reconstructed
    )
    results = [
        LookupResult(query="selected", selected=selected),
        LookupResult(query="matches", matches=[match("matched")]),
    ]
    monkeypatch.setattr(handlers, "_mtime_ns", lambda _path: 1)

    run(handlers._warm_cache(indexer, results, lookup))

    assert indexer._cache.get("selected") == selected
    assert indexer._cache.get("matched") == reconstructed


def test_warm_cache_skips_reconstruct_errors_invalid_values_and_changed_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexer = FakeIndexer()
    values = {"bad": RuntimeError("boom"), "invalid": object(), "changed": code("changed")}

    def reconstruct(symbol_id: str) -> Any:
        value = values[symbol_id]
        if isinstance(value, BaseException):
            raise value
        return value

    calls = iter([1, 2, 3, 4])
    monkeypatch.setattr(handlers, "_mtime_ns", lambda _path: next(calls))
    results = [LookupResult(query="x", matches=[match("bad"), match("invalid"), match("changed")])]
    run(handlers._warm_cache(indexer, results, SimpleNamespace(reconstruct=reconstruct)))

    assert indexer._cache.get("bad") is None
    assert indexer._cache.get("invalid") is None
    assert indexer._cache.get("changed") is None


def test_handle_symbol_lookup_reports_readiness_configuration_and_input_errors() -> None:
    not_ready = FakeIndexer(ready=False)
    assert run(handlers.handle_symbol_lookup(not_ready, "x", None))[0]["__xx_e_code"] == "INDEX_UNAVAILABLE"
    assert run(handlers.handle_symbol_lookup(FakeIndexer(), "x", None))[0]["__xx_e_code"] == "LOOKUP_UNAVAILABLE"
    assert run(handlers.handle_symbol_lookup(FakeIndexer(), "x", object()))[0]["__xx_e_code"] == "LOOKUP_UNAVAILABLE"

    lookup = lookup_object()
    result = run(handlers.handle_symbol_lookup(FakeIndexer(), 123, lookup))
    assert result[0]["__xx_e_code"] == "INVALID_LOOKUP_NAME"


def test_handle_symbol_lookup_handles_single_list_and_lookup_exceptions() -> None:
    one = LookupResult(query="one", matches=[match()])
    two = LookupResult(query="two", matches=[match("id-2")])
    obj = lookup_object({"one": one, "two": two})
    indexer = FakeIndexer()
    handlers._warm_tasks.clear()

    async def scenario() -> tuple[Any, Any, Any]:
        single = await handlers.handle_symbol_lookup(indexer, "one", obj)
        many = await handlers.handle_symbol_lookup(indexer, ["one", "missing", "two"], obj)
        empty = await handlers.handle_symbol_lookup(indexer, [], obj)
        await handlers.drain_warm_tasks()
        return single, many, empty

    single, many, empty = run(scenario())
    assert single["one"][0]["symbol_id"] == "id-1"
    assert set(many) == {"one", "two"}
    assert empty == {}


def test_handle_symbol_lookup_converts_lookup_exception_to_not_found() -> None:
    obj = object.__new__(SymbolLookup)
    obj.lookup = lambda _name: (_ for _ in ()).throw(RuntimeError("bad"))  # type: ignore[attr-defined]
    result = run(handlers.handle_symbol_lookup(FakeIndexer(), "missing", obj))
    assert result == [
        {"__xx_e_code": "SYMBOL_NOT_FOUND", "message": "symbol doesn't exist of index. Use `grep`"}
    ]


def test_handle_symbol_lookup_does_not_schedule_warming_while_draining() -> None:
    obj = lookup_object({"x": LookupResult(query="x", matches=[match()])})
    old = handlers._warm_draining
    handlers._warm_draining = True
    try:
        result = run(handlers.handle_symbol_lookup(FakeIndexer(), "x", obj))
        assert result["x"][0]["symbol_id"] == "id-1"
        assert not handlers._warm_tasks
    finally:
        handlers._warm_draining = old


def test_handle_get_symbol_reports_readiness_and_id_validation() -> None:
    lookup = SimpleNamespace(reconstruct=lambda _id: code())
    assert run(handlers.handle_get_symbol(FakeIndexer(False), "id", lookup))["__xx_e_code"] == "INDEX_UNAVAILABLE"
    invalid = run(handlers.handle_get_symbol(FakeIndexer(), ["ok", 1], lookup))
    assert invalid["__xx_e_code"] == "INVALID_SYMBOL_ID"
    assert run(handlers.handle_get_symbol(FakeIndexer(), 1, lookup))["__xx_e_code"] == "INVALID_SYMBOL_ID"


def test_handle_get_symbol_loads_caches_and_supports_multiple_ids() -> None:
    values = {"one": code("one"), "two": code("two", text="second\n")}
    lookup = SimpleNamespace(reconstruct=lambda symbol_id: values[symbol_id])
    indexer = FakeIndexer()

    result = run(handlers.handle_get_symbol(indexer, ["one", "two"], lookup))

    assert [item["symbol_id"] for item in result] == ["one", "two"]
    assert indexer._cache.get("one") == values["one"]
    assert indexer._cache.get("two") == values["two"]


def test_handle_get_symbol_reports_pending_cached_and_reconstructed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = code("cached")
    indexer = FakeIndexer()
    indexer._cache.put(cached.id, cached.path, cached)
    lookup = SimpleNamespace(reconstruct=lambda _id: code("fresh"))
    monkeypatch.setattr(handlers, "wait_for_path_ready", _always_false)

    cached_result = run(handlers.handle_get_symbol(indexer, "cached", lookup))
    fresh_result = run(handlers.handle_get_symbol(indexer, "fresh", lookup))
    assert cached_result["__xx_e_code"] == "PATH_PENDING"
    assert fresh_result["__xx_e_code"] == "PATH_PENDING"


async def _always_false(*_args: Any, **_kwargs: Any) -> bool:
    return False


def test_handle_get_symbol_reports_lookup_errors_and_preserves_single_shape() -> None:
    indexer = FakeIndexer()
    lookup = SimpleNamespace(reconstruct=lambda _id: (_ for _ in ()).throw(KeyError("missing")))

    result = run(handlers.handle_get_symbol(indexer, "missing", lookup))

    assert result == {"__xx_e_code": "SYMBOL_NOT_FOUND", "message": "'missing'"}
    assert run(handlers.handle_get_symbol(indexer, [], lookup)) == []


def test_handle_get_symbol_range_validates_and_clips_to_symbol() -> None:
    value = code("id", "one\ntwo\nthree\nfour\n", start=10, end=13)
    lookup = SimpleNamespace(reconstruct=lambda _id: value)
    indexer = FakeIndexer()

    result = run(handlers.handle_get_symbol_range(indexer, "id", 11, 99, lookup))

    assert result["start_line"] == 11
    assert result["end_line"] == 13
    assert result["code"] == "two\nthree\nfour\n"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(0, 1, "INVALID_RANGE"), (3, 2, "INVALID_RANGE"), (99, 100, "RANGE_EMPTY")],
)
def test_handle_get_symbol_range_rejects_invalid_or_empty_ranges(
    start: int,
    end: int,
    expected: str,
) -> None:
    value = code()
    lookup = SimpleNamespace(reconstruct=lambda _id: value)
    result = run(handlers.handle_get_symbol_range(FakeIndexer(), "id", start, end, lookup))
    assert result["__xx_e_code"] == expected


def test_handle_get_symbol_range_supports_lists_and_pending_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {"one": code("one"), "two": code("two")}
    lookup = SimpleNamespace(reconstruct=lambda symbol_id: values[symbol_id])
    indexer = FakeIndexer()
    monkeypatch.setattr(handlers, "wait_for_path_ready", _always_false)

    result = run(handlers.handle_get_symbol_range(indexer, ["one", "two"], 1, 2, lookup))

    assert [item["__xx_e_code"] for item in result] == ["PATH_PENDING", "PATH_PENDING"]

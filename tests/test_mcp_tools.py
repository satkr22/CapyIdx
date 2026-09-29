from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from capyidx.mcp import handlers
from capyidx.mcp import tools as mcp_tools
from capyidx.retrieval.models import SymbolCode


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def symbol() -> SymbolCode:
    return SymbolCode(
        id="id-1",
        name="hello",
        type="function",
        path="src/hello.py",
        start_line=2,
        end_line=4,
        code="def hello():\n    return 'hi'\n",
    )


def runtime() -> SimpleNamespace:
    return SimpleNamespace(indexer=object(), lookup=object())


def test_tool_schema_has_mcp_shape_and_registry_is_consistent() -> None:
    assert [tool.name for tool in mcp_tools.TOOLS] == [
        "lookup_symbol",
        "get_symbol",
        "get_symbol_range",
    ]
    assert set(mcp_tools.TOOLS_BY_NAME) == {tool.name for tool in mcp_tools.TOOLS}
    for tool in mcp_tools.TOOLS:
        schema = tool.schema()
        assert schema["name"] == tool.name
        assert schema["description"] == tool.description
        assert schema["inputSchema"] is tool.input_schema


def test_tool_definitions_require_expected_fields_and_disallow_extras() -> None:
    lookup, get, ranged = mcp_tools.TOOLS
    assert lookup.input_schema["required"] == ["name"]
    assert get.input_schema["required"] == ["symbol_id"]
    assert ranged.input_schema["required"] == ["symbol_id", "start_line", "end_line"]
    for tool in mcp_tools.TOOLS:
        assert tool.input_schema["additionalProperties"] is False
    assert lookup.input_schema["properties"]["name"]["oneOf"][1]["minItems"] == 1


def test_text_envelope_defaults_to_success_and_can_be_error() -> None:
    assert mcp_tools._text("ok") == {
        "content": [{"type": "text", "text": "ok"}],
        "isError": False,
    }
    assert mcp_tools._text("bad", is_error=True)["isError"] is True


def test_exec_lookup_formats_success_and_top_level_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup_success(*_args: Any) -> dict[str, list[dict[str, object]]]:
        return {"hello": [{"symbol_id": "id", "path": "a.py", "start_line": 1, "end_line": 2}]}

    async def lookup_error(*_args: Any) -> dict[str, object]:
        return {"__xx_e_code": "E", "message": "failed"}

    monkeypatch.setattr(handlers, "handle_symbol_lookup", lookup_success)
    success = run(mcp_tools._exec_lookup(runtime(), {"name": "hello"}))
    assert success["isError"] is False
    assert "query: hello" in success["content"][0]["text"]

    monkeypatch.setattr(handlers, "handle_symbol_lookup", lookup_error)
    failure = run(mcp_tools._exec_lookup(runtime(), {"name": "hello"}))
    assert failure == {
        "content": [{"type": "text", "text": "[error E] failed"}],
        "isError": True,
    }


def test_exec_get_and_range_format_single_error_and_multiple_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = {
        "symbol_id": "id",
        "symbol_name": "hello",
        "path": "a.py",
        "start_line": 1,
        "end_line": 1,
        "code": "pass",
    }

    async def get(*_args: Any, **_kwargs: Any) -> Any:
        return [value, value]

    async def ranged(*_args: Any, **_kwargs: Any) -> Any:
        return {"__xx_e_code": "RANGE_EMPTY", "message": "outside"}

    monkeypatch.setattr(handlers, "handle_get_symbol", get)
    multiple = run(mcp_tools._exec_get(runtime(), {"symbol_id": ["a", "b"]}))
    assert multiple["isError"] is False
    assert multiple["content"][0]["text"].count("id: id") == 2

    monkeypatch.setattr(handlers, "handle_get_symbol_range", ranged)
    error_result = run(
        mcp_tools._exec_range(
            runtime(), {"symbol_id": "a", "start_line": 5, "end_line": 6}
        )
    )
    assert error_result["isError"] is True
    assert "RANGE_EMPTY" in error_result["content"][0]["text"]


def test_exec_get_marks_only_a_single_error_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = {"__xx_e_code": "E", "message": "bad"}

    async def get(*_args: Any, **_kwargs: Any) -> Any:
        return error

    monkeypatch.setattr(handlers, "handle_get_symbol", get)
    result = run(mcp_tools._exec_get(runtime(), {"symbol_id": "a"}))
    assert result["isError"] is True

    async def two_errors(*_args: Any, **_kwargs: Any) -> Any:
        return [error, error]

    monkeypatch.setattr(handlers, "handle_get_symbol", two_errors)
    result = run(mcp_tools._exec_get(runtime(), {"symbol_id": ["a", "b"]}))
    assert result["isError"] is False


def test_exec_get_passes_default_limit_and_exec_range_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    async def get(*args: Any, **kwargs: Any) -> dict[str, object]:
        observed["get"] = (args, kwargs)
        return {"symbol_id": "id", "path": "a.py", "start_line": 1, "end_line": 1, "code": "x"}

    async def ranged(*args: Any, **kwargs: Any) -> dict[str, object]:
        observed["range"] = (args, kwargs)
        return {"symbol_id": "id", "path": "a.py", "start_line": 1, "end_line": 1, "code": "x"}

    monkeypatch.setattr(handlers, "handle_get_symbol", get)
    monkeypatch.setattr(handlers, "handle_get_symbol_range", ranged)
    run(mcp_tools._exec_get(runtime(), {"symbol_id": "id"}))
    run(mcp_tools._exec_range(runtime(), {"symbol_id": "id", "start_line": 1, "end_line": 2}))

    assert observed["get"][1]["char_limit"] == handlers.DEFAULT_CHAR_LIMIT
    assert observed["range"][1]["char_limit"] == handlers.DEFAULT_CHAR_LIMIT

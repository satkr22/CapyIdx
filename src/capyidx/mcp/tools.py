from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from capyidx.mcp import handlers, format as fmt
from capyidx.mcp.runtime import Runtime

Executor = Callable[[Runtime, dict[str, Any]], Awaitable[dict[str, Any]]]


# // tools/list returns:
# {"tools": [{"name": "...", "description": "...", "inputSchema": {...}}, ...]}

# // tools/call returns:
# {"content": [{"type": "text", "text": "<string>"}], "isError": false}

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    execute: Executor

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


def _text(payload: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": payload}], "isError": is_error}


_ID_OR_LIST = {
    "oneOf": [
        {"type": "string"},
        {"type": "array", "items": {"type": "string"}, "minItems": 1},
    ]
}


# ---------- executors ----------

async def _exec_lookup(rt: Runtime, args: dict[str, Any]) -> dict[str, Any]:
    result = await handlers.handle_symbol_lookup(rt.indexer, args["name"], rt.lookup)
    # Lookup itself returns per-query error dicts; check the top-level envelope.
    if isinstance(result, dict) and "__xx_e_code" in result.keys():
        return _text(f"[error {result['__xx_e_code']}] {result['message']}", is_error=True)
    return _text(fmt.format_lookup(result))

async def _exec_get(rt: Runtime, args: dict[str, Any]) -> dict[str, Any]:
    result = await handlers.handle_get_symbol(
        rt.indexer,
        args["symbol_id"],
        rt.lookup,
        char_limit=handlers.DEFAULT_CHAR_LIMIT,
    )
    items = result if isinstance(result, list) else [result]
    text = fmt.format_symbols(items)
    is_err = len(items) == 1 and fmt._is_error(items[0])
    return _text(text, is_error=is_err)


async def _exec_range(rt: Runtime, args: dict[str, Any]) -> dict[str, Any]:
    result = await handlers.handle_get_symbol_range(
        rt.indexer,
        args["symbol_id"],
        args["start_line"],
        args["end_line"],
        rt.lookup,
        char_limit=handlers.DEFAULT_CHAR_LIMIT,
    )
    items = result if isinstance(result, list) else [result]
    text = fmt.format_symbols(items)
    is_err = len(items) == 1 and fmt._is_error(items[0])
    return _text(text, is_error=is_err)


# ---------- registry ----------

TOOLS: list[Tool] = [
    Tool(
        name="lookup_symbol",
        description=(
            "Search the codebase for symbols (functions, methods, classes) by name. "
            "Returns candidate symbol IDs with file path and line range. "
            "Call this first when you don't already have a symbol_id. "
            "Accepts a single name or a list to look up several at once."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    **_ID_OR_LIST,
                    "description": "Symbol name, or list of names.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        execute=_exec_lookup,
    ),
    Tool(
        name="get_symbol",
        description=(
            "Fetch the full source of a symbol by symbol_id from lookup_symbol. "
            "Returns path, line range, and code. "
            "If the result is truncated, use get_symbol_range to read more."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol_id": {
                    **_ID_OR_LIST, 
                    "description": "ID from lookup_symbol."
                },
            },
            "required": ["symbol_id"],
            "additionalProperties": False,
        },
        execute=_exec_get,
    ),
    Tool(
        name="get_symbol_range",
        description=(
            "Fetch a line range within a symbol. Use when get_symbol was truncated"
            "Line numbers are absolute file lines, as shown in get_symbol's header."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol_id": {
                    **_ID_OR_LIST, 
                    "description": "ID from lookup_symbol."
                },
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["symbol_id", "start_line", "end_line"],
            "additionalProperties": False,
        },
        execute=_exec_range,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}
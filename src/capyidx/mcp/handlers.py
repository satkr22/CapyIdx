from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import replace
from typing import Any

from capyidx.indexer.codebase_indexer import CodeIndexer
from capyidx.mcp.cache import canonical_path
from capyidx.retrieval.models import SymbolCode


DEFAULT_CHAR_LIMIT = 12_000


def error(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message}


def _path_is_pending(indexer: CodeIndexer, path: str) -> bool:
    target = canonical_path(path)
    return any(
        pending == path or canonical_path(pending) == target
        for pending in indexer.pending_paths
    )


async def wait_for_path_ready(
    indexer: CodeIndexer,
    path: str,
    max_wait_s: float = 1.0,
    poll_s: float = 0.5,
) -> bool:
    """Wait only for this path; unrelated file refreshes do not block it."""
    if max_wait_s < 0 or poll_s <= 0:
        raise ValueError("max_wait_s must be non-negative and poll_s must be positive")

    deadline = time.monotonic() + max_wait_s
    while _path_is_pending(indexer, path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_s, remaining))
    return True


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _load_symbol(
    indexer: CodeIndexer,
    symbol_id: str,
    lookup: Any | None,
) -> SymbolCode | dict[str, object]:
    cached = indexer._cache.get(symbol_id)
    if cached is not None:
        return cached

    if lookup is None:
        lookup = getattr(indexer, "symbol_lookup", None)
    if lookup is None:
        return error("LOOKUP_UNAVAILABLE", "symbol lookup is not configured")

    try:
        code = await _maybe_await(lookup.reconstruct(symbol_id))
    except KeyError as exc:
        return error("SYMBOL_NOT_FOUND", str(exc))
    if not isinstance(code, SymbolCode):
        return error("LOOKUP_INVALID", "symbol lookup returned an invalid symbol")
    indexer._cache.put(symbol_id, code.path, code)
    return code


def _truncate_text(code: SymbolCode, *, char_limit: int) -> dict[str, object]:
    if char_limit <= 0:
        raise ValueError("char_limit must be positive")

    text = code.code
    if len(text) <= char_limit:
        return {"id": code.id, "code": text}

    marker = "...(truncated)..."
    if char_limit <= len(marker):
        return {
            "id": code.id,
            "code": marker[:char_limit],
            "truncated": True,
            "returned_end_line": code.start_line - 1,
            "total_lines": len(text.splitlines()),
        }

    budget = char_limit - len(marker)
    prefix_parts: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        if used + len(line) > budget:
            break
        prefix_parts.append(line)
        used += len(line)

    prefix = "".join(prefix_parts)
    if not prefix and budget:
        prefix = text[:budget]

    returned_end = code.start_line + max(0, len(prefix.splitlines()) - 1)
    return {
        "id": code.id,
        "code": prefix + marker,
        "truncated": True,
        "returned_end_line": returned_end,
        "total_lines": len(text.splitlines()),
    }


def serialize_symbol_code(
    code: SymbolCode,
    *,
    char_limit: int = DEFAULT_CHAR_LIMIT,
) -> dict[str, object]:
    """Serialize a symbol with follow-up metadata only when truncated."""
    return _truncate_text(code, char_limit=char_limit)


def _lookup_response(result: Any) -> dict[str, object]:
    """Convert a lookup result into the MCP response-object contract."""
    if isinstance(result, dict):
        return dict(result)

    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        if isinstance(payload, dict):
            return dict(payload)

    return error(
        "LOOKUP_INVALID_RESPONSE",
        "symbol lookup did not return a response dictionary",
    )


async def handle_symbol_lookup(
    indexer: CodeIndexer,
    name: str | dict[str, object] | None = None,
    lookup: Any | None = None,
    **lookup_args: Any,
) -> dict[str, object]:
    """Return the symbol lookup result as a plain MCP response dictionary."""
    if not indexer.system_ready:
        return error("INDEX_UNAVAILABLE", "Initial indexing or branch reindex in progress")

    if isinstance(name, dict):
        args = dict(name)
        parsed_name = args.pop(
            "name",
            args.pop("query", args.pop("symbol_name", None)),
        )
        if isinstance(parsed_name, (str, dict)) or parsed_name is None:
            name = parsed_name
        else:
            name = None
        lookup_args = {**args, **lookup_args}

    if lookup is None:
        lookup = getattr(indexer, "symbol_lookup", None)
    if lookup is None or name is None:
        return error("LOOKUP_UNAVAILABLE", "symbol lookup is not configured")

    try:
        result = await _maybe_await(lookup.lookup(name, **lookup_args))
    except TypeError:
        if not lookup_args:
            raise
        result = await _maybe_await(lookup.lookup(name))
    return _lookup_response(result)


async def handle_lookup_symbol(
    indexer: CodeIndexer,
    name: str | dict[str, object] | None = None,
    lookup: Any | None = None,
    **lookup_args: Any,
) -> dict[str, object]:
    """Backward-compatible alias for handle_symbol_lookup."""
    return await handle_symbol_lookup(indexer, name, lookup, **lookup_args)


async def handle_get_symbol(
    indexer: CodeIndexer,
    symbol_id: str,
    path: str,
    lookup: Any | None = None,
    char_limit: int = DEFAULT_CHAR_LIMIT,
) -> dict[str, object]:
    if not indexer.system_ready:
        return error("INDEX_UNAVAILABLE", "Initial indexing or branch reindex in progress")

    if not await wait_for_path_ready(indexer, path):
        return error("PATH_PENDING", "still reindexing this file, try again shortly or use grep")

    code = await _load_symbol(indexer, symbol_id, lookup)
    if isinstance(code, dict):
        return code
    return serialize_symbol_code(code, char_limit=char_limit)


async def handle_get_symbol_range(
    indexer: CodeIndexer,
    symbol_id: str,
    start_line: int,
    end_line: int,
    path: str | None = None,
    lookup: Any | None = None,
    char_limit: int = DEFAULT_CHAR_LIMIT,
) -> dict[str, object]:
    if not indexer.system_ready:
        return error("INDEX_UNAVAILABLE", "Initial indexing or branch reindex in progress")
    if start_line < 1 or end_line < start_line:
        return error("INVALID_RANGE", "start_line and end_line must be positive and ordered")

    code = await _load_symbol(indexer, symbol_id, lookup)
    if isinstance(code, dict):
        return code

    target_path = path or code.path
    if not await wait_for_path_ready(indexer, target_path):
        return error("PATH_PENDING", "still reindexing this file, try again shortly or use grep")

    actual_start = max(start_line, code.start_line)
    actual_end = min(end_line, code.end_line)
    if actual_start > actual_end:
        return error("RANGE_EMPTY", "requested range is outside the symbol")

    lines = code.code.splitlines(keepends=True)
    first = actual_start - code.start_line
    last = actual_end - code.start_line + 1
    selected = "".join(lines[first:last])
    ranged = replace(
        code,
        code=selected,
        start_line=actual_start,
        end_line=actual_end,
    )
    response = serialize_symbol_code(ranged, char_limit=char_limit)
    response["start_line"] = actual_start
    response["end_line"] = actual_end
    return response

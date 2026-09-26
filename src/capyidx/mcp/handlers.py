from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import replace
from typing import Any
from pathlib import Path

from capyidx.indexer.codebase_indexer import CodeIndexer
from capyidx.mcp.cache import canonical_path
from capyidx.retrieval.models import SymbolCode, LookupResult
from capyidx.retrieval.retrieval_pipeline import SymbolLookup


DEFAULT_CHAR_LIMIT = 12_000

# Holds references to in-flight cache-warming tasks so they aren't GC'd
# before they run. Tasks remove themselves on completion.
_warm_tasks: set[asyncio.Task] = set()

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
    lookup: SymbolLookup
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
    
    return code


def _symbol_metadata(code: SymbolCode) -> dict[str, object]:
    """Return the stable metadata shared by full and ranged responses."""
    return {
        "symbol_id": code.id,
        "symbol_name": code.name,
        "path": code.path,
        "start_line": code.start_line,
        "end_line": code.end_line,
    }


def _truncate_text(code: SymbolCode, *, char_limit: int) -> dict[str, object]:
    if char_limit <= 0:
        raise ValueError("char_limit must be positive")

    text = code.code
    if len(text) <= char_limit:
        return {**_symbol_metadata(code), "code": text}

    marker = "...(truncated)..."
    if char_limit <= len(marker):
        return {
            **_symbol_metadata(code),
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
        **_symbol_metadata(code),
        "truncated": True,
        "returned_end_line": returned_end,
        "total_lines": len(text.splitlines()),
        "code": prefix + marker,
    }


def serialize_symbol_code(
    code: SymbolCode,
    *,
    char_limit: int = DEFAULT_CHAR_LIMIT,
) -> dict[str, object]:
    """Serialize a symbol with follow-up metadata only when truncated."""
    return _truncate_text(code, char_limit=char_limit)


def _lookup_response(results: list[LookupResult]) -> dict[str, list[dict[str, object]]]:
    """Convert a list of lookup result into the MCP response-object contract."""
    if not isinstance(results, list):
        return error(
            "LOOKUP_INVALID_RESPONSE",
            "symbol lookup did not return a response dictionary",
        )
        
    res: dict[str, list[dict[str, object]]] = {}
    for result in results:
        selected = result.selected
        if selected is not None:
            res[result.query] = [
                { 
                    "symbol_id": selected.id,
                    "path": selected.path,
                    "start_line": selected.start_line,
                    "end_line": selected.end_line,
                }
            ]
            continue

        matches = result.matches
        match_res:list[dict[str, object]] = []
        if matches is not None:
            for match in matches:
                match_res.append({
                    "symbol_id": match.id,
                    "path": match.path,
                    "start_line": match.start_line,
                    "end_line": match.end_line,
                })
            res[result.query] = match_res
    
        elif matches is None or len(matches) == 0:
            err = error(
               "SYMBOL_NOT_FOUND", 
               "symbol doesn't exist of index. Use `grep`"
            )
            if result.query is not None:
                res[result.query] = [err]
            continue
    return res

def _mtime_ns(path: str) -> int | None:
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None

async def _warm_cache(
    indexer: CodeIndexer,
    results: list[LookupResult],
    lookup: SymbolLookup
) -> None:
    """Best-effort cache warming; skips entries whose file changed mid-reconstruct."""
    for lookup_result in results: 
        selected = lookup_result.selected
        if selected is not None:
            if isinstance(selected, SymbolCode):
                indexer._cache.put(selected.id, selected.path, selected)
            continue
            
        elif lookup_result.matches:
            for match in lookup_result.matches:
                before = _mtime_ns(match.path)
                try:
                    code = await _maybe_await(lookup.reconstruct(match.id))
                except Exception:
                    continue
                
                if not isinstance(code, SymbolCode):
                    continue
                
                if _mtime_ns(code.path) != before:
                    continue # NOTE: file changed during reconstruct -> stale
                indexer._cache.put(code.id, code.path, code)      
        else:
            pass

async def handle_symbol_lookup(
    indexer: CodeIndexer,
    name: str | list[str],
    lookup: SymbolLookup,
) -> dict[str, list[dict[str, object]]]:
    """Return the symbol lookup result as a plain MCP response dictionary."""
    
    res: dict[str, list[dict[str, object]]] = {}

    if not indexer.system_ready:
        res["indexer"] = [
            error("INDEX_UNAVAILABLE", "Initial indexing or branch reindex in progress")
        ]
        return res

    if lookup is None:
        res["indexer"] = [
            error("LOOKUP_UNAVAILABLE", "symbol lookup is not configured")
        ]
        return res
    
    if isinstance(lookup, SymbolLookup):
        # lookup object
        lookup_obj = lookup
    
    if isinstance(name, str):
        # single lookup
        try:
            result = [lookup_obj.lookup(name)]
        except Exception:
            res["indexer"] = [
                error("SYMBOL_NOT_FOUND", "symbol doesn't exist of index. Use `grep`")
            ]
            return res
           
    elif isinstance(name, list):
        # list lookup
        look_list:list[LookupResult] = []
        for symbol in name:
            try:
                look_list.append(lookup_obj.lookup(symbol))
            except Exception:
                # TODO: add logging here
                continue
        result = look_list
    else:
        res["indexer"] = [
            error("INVALID_LOOKUP_NAME", "name must be a string or list of strings")
        ]
        return res
    
    # cache would be warmed up in background not blocking clients request for lookup (Fire-and-forget cache warming)
    
    cache_task = asyncio.create_task(_warm_cache(indexer, result, lookup_obj))
    _warm_tasks.add(cache_task)
    cache_task.add_done_callback(_warm_tasks.discard)
    
    return _lookup_response(result)


async def handle_get_symbol(
    indexer: CodeIndexer,
    symbol_id: str | list[str],
    lookup: SymbolLookup,
    path: str | None = None,
    char_limit: int = DEFAULT_CHAR_LIMIT,
) -> dict[str, object] | list[dict[str, object]]:
    
    if not indexer.system_ready:
        return error("INDEX_UNAVAILABLE", "Initial indexing or branch reindex in progress")

    if isinstance(symbol_id, str):
        symbol_ids = [symbol_id]
        single = True
    elif isinstance(symbol_id, list) and all(isinstance(item, str) for item in symbol_id):
        symbol_ids = symbol_id
        single = False
    else:
        return error("INVALID_SYMBOL_ID", "symbol_id must be a string or list of strings")

    responses: list[dict[str, object]] = []
    for current_id in symbol_ids:
        current_path = path
        if current_path is not None and not await wait_for_path_ready(indexer, current_path):
            responses.append(
                error(
                    "PATH_PENDING",
                    "still reindexing this file, try again shortly or use grep",
                )
            )
            continue

        code = await _load_symbol(indexer, current_id, lookup)
        if isinstance(code, dict):
            responses.append(code)
            continue

        # A list may contain symbols from different files. When no path was
        # supplied, wait for each symbol's own path after loading it.
        if current_path is None and not await wait_for_path_ready(indexer, code.path):
            responses.append(
                error(
                    "PATH_PENDING",
                    "still reindexing this file, try again shortly or use grep",
                )
            )
            continue
        responses.append(serialize_symbol_code(code, char_limit=char_limit))

    return responses[0] if single else responses


async def handle_get_symbol_range(
    indexer: CodeIndexer,
    symbol_id: str | list[str],
    start_line: int,
    end_line: int,
    lookup: SymbolLookup,
    path: str | None = None,
    char_limit: int = DEFAULT_CHAR_LIMIT,
) -> dict[str, object] | list[dict[str, object]]:
    if not indexer.system_ready:
        return error("INDEX_UNAVAILABLE", "Initial indexing or branch reindex in progress")
    if start_line < 1 or end_line < start_line:
        return error("INVALID_RANGE", "start_line and end_line must be positive and ordered")

    if isinstance(symbol_id, str):
        symbol_ids = [symbol_id]
        single = True
    elif isinstance(symbol_id, list) and all(isinstance(item, str) for item in symbol_id):
        symbol_ids = symbol_id
        single = False
    else:
        return error("INVALID_SYMBOL_ID", "symbol_id must be a string or list of strings")

    responses: list[dict[str, object]] = []
    for current_id in symbol_ids:
        code = await _load_symbol(indexer, current_id, lookup)
        if isinstance(code, dict):
            responses.append(code)
            continue

        target_path = path or code.path
        if not await wait_for_path_ready(indexer, target_path):
            responses.append(
                error(
                    "PATH_PENDING",
                    "still reindexing this file, try again shortly or use grep",
                )
            )
            continue

        actual_start = max(start_line, code.start_line)
        actual_end = min(end_line, code.end_line)
        if actual_start > actual_end:
            responses.append(error("RANGE_EMPTY", "requested range is outside the symbol"))
            continue

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
        responses.append(serialize_symbol_code(ranged, char_limit=char_limit))

    return responses[0] if single else responses

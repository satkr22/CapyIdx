from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Union

from capyidx.api import _setup
from capyidx.chunker.chunk_codebase_index import ChunkCodebaseIndex
from capyidx.db.db import open_index
from capyidx.indexer.codebase_indexer import CodeIndexer
from capyidx.retrieval.retrieval_pipeline import SymbolLookup


PathLike = Union[str, Path]

MCP_TOOLS = ("lookup_symbol", "get_symbol", "get_symbol_range")


def mcp_handshake(
    emit: Callable[[dict[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Return/emit lifecycle messages before repository indexing begins."""
    messages = [
        {"method": "initialize", "result": {"server": "capyidx"}},
        {"method": "notifications/initialized"},
        {"method": "tools/list", "result": {"tools": list(MCP_TOOLS)}},
    ]
    if emit is not None:
        for message in messages:
            emit(message)
    return messages


async def mcp_start(
    repo: PathLike,
    *,
    emit_message: Callable[[dict[str, Any]], Any] | None = None,
    flush_interval: float = 3.0,
):
    """Start the MCP runtime and keep indexing/watch tasks alive."""
    fs, roots, tags = await _setup(repo)
    conn = open_index(tags)
    watch_task: asyncio.Task[None] | None = None
    branch_tasks: list[asyncio.Task[None]] = []

    async def emit_progress(update: Any) -> None:
        if emit_message is None:
            return
        data = asdict(update) if is_dataclass(update) else update # type: ignore
        message = {
            "method": "notifications/message",
            "params": {
                "level": "info",
                "data": data,
            },
        }
        result = emit_message(message)
        if inspect.isawaitable(result):
            await result

    try:
        chunk_index = ChunkCodebaseIndex(
            db=conn,
            filesystem=fs,
            max_chunk_size=512,
        )
        indexer = CodeIndexer(fs=fs, indexes=[chunk_index])
        indexer.symbol_lookup = SymbolLookup(conn, roots)  # type: ignore[attr-defined]

        # MCP lifecycle responses do not wait for indexing.
        mcp_handshake(emit_message)

        async def consume_watch() -> None:
            while True:
                async for _ in indexer.start_watch(
                    db=conn,
                    workspace_dirs=roots,
                    flush_interval=flush_interval,
                ):
                    pass
                if not await indexer.wait_for_watch_restart():
                    return

        watch_task = asyncio.create_task(consume_watch())
        while indexer._watch_started is None:
            await asyncio.sleep(0)
        await indexer._watch_started.wait()

        # HEAD polling is separate from file-event polling so a large edit
        # cannot be mistaken for a branch switch.
        for root in roots:
            branch_tasks.append(
                asyncio.create_task(
                    indexer.watch_git_head(
                        root,
                        conn,
                        workspace_dirs=roots,
                        progress_callback=emit_progress,
                    )
                )
            )

        indexer.set_system_ready(False)
        async for update in indexer.refresh_codebase_index(roots, conn):
            await emit_progress(update)
            yield update
        if indexer.current_indexing_state.status == "done":
            indexer.set_system_ready(True)

        await watch_task
    finally:
        for task in branch_tasks:
            task.cancel()
        if branch_tasks:
            await asyncio.gather(*branch_tasks, return_exceptions=True)

        if watch_task is not None and not watch_task.done():
            watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass
        conn.close()

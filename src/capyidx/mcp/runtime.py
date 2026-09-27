# capyidx/mcp/runtime.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable
import sqlite3

from capyidx.api import _setup
from capyidx.chunker.chunk_codebase_index import ChunkCodebaseIndex
from capyidx.db.db import open_index
from capyidx.indexer.codebase_indexer import CodeIndexer
from capyidx.retrieval.retrieval_pipeline import SymbolLookup
from capyidx.mcp.handlers import drain_warm_tasks


@dataclass
class Runtime:
    indexer: CodeIndexer
    lookup: SymbolLookup
    conn: sqlite3.Connection
    roots: list[str]
    watch_task: asyncio.Task
    branch_tasks: list[asyncio.Task]

    async def shutdown(self) -> None:
        # Drain warm-cache tasks first, they are the only ones that
        # actively query the connection and would race with conn.close()
        await drain_warm_tasks(timeout=2.0)
        
        # stop the long-lived watchers as they also hold conn
        for t in self.branch_tasks:
            t.cancel()
        if self.branch_tasks:
            await asyncio.gather(*self.branch_tasks, return_exceptions=True)
        if not self.watch_task.done():
            self.watch_task.cancel()
            try:
                await self.watch_task
            except asyncio.CancelledError:
                pass
        self.conn.close()


async def build_runtime(
    repo: str | Path,
    emit_message: Callable[[dict[str, Any]], Any] | None = None,
    flush_interval: float = 3.0,
) -> Runtime:
    fs, roots, tags = await _setup(repo)
    conn = open_index(tags)

    chunk_index = ChunkCodebaseIndex(db=conn, filesystem=fs, max_chunk_size=512)
    indexer = CodeIndexer(fs=fs, indexes=[chunk_index])
    lookup = SymbolLookup(conn, roots)

    async def emit_progress(update: Any) -> None:
        if emit_message is None:
            return
        data = asdict(update) if is_dataclass(update) else update # type: ignore
        result = emit_message({
            "method": "notifications/message",
            "params": {"level": "info", "data": data},
        })
        if asyncio.iscoroutine(result):
            await result

    # --- file watch (with cache invalidation already happening inside start_watch()) ---
    async def consume_watch() -> None:
        while True:
            async for event in indexer.start_watch(
                db=conn, workspace_dirs=roots, flush_interval=flush_interval,
            ):
                pass
            if not await indexer.wait_for_watch_restart():
                return

    watch_task = asyncio.create_task(consume_watch())
    while indexer._watch_started is None:
        await asyncio.sleep(0)
    await indexer._watch_started.wait()

    # --- branch head watchers ---
    branch_tasks = [
        asyncio.create_task(indexer.watch_git_head(
            root, conn, workspace_dirs=roots, progress_callback=emit_progress,
        ))
        for root in roots
    ]

    # --- initial index runs in background; tools check system_ready ---
    async def initial_index() -> None:
        indexer.set_system_ready(False)
        async for update in indexer.refresh_codebase_index(roots, conn):
            await emit_progress(update)
        if indexer.current_indexing_state.status == "done":
            indexer.set_system_ready(True)

    asyncio.create_task(initial_index())

    return Runtime(indexer, lookup, conn, roots, watch_task, branch_tasks)
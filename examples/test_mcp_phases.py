"""Exercise CapyIdx MCP phases 0-10 against a real Git repository.

This validates the indexing/runtime layer implemented for the MCP server. It
does not require an MCP transport because the actual protocol server is the
next integration layer.

Usage:
    PYTHONPATH=src python examples/test_mcp_phases.py /path/to/repository

The script appends a temporary comment to one indexed source file and restores
the original bytes in a finally block. It never runs git checkout/switch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse

from capyidx.base.index_d import IndexingProgressUpdate
from capyidx.chunker.chunk_codebase_index import ChunkCodebaseIndex
from capyidx.db.db import open_index
from capyidx.indexer.codebase_indexer import CodeIndexer
from capyidx.mcp.cache import SymbolCache, canonical_path
from capyidx.mcp.handlers import (
    handle_get_symbol,
    handle_get_symbol_range,
    handle_symbol_lookup,
    serialize_symbol_code,
    wait_for_path_ready,
)
from capyidx.mcp.startup import MCP_TOOLS, mcp_handshake
from capyidx.retrieval.models import SymbolCode
from capyidx.retrieval.retrieval_pipeline import SymbolLookup
from capyidx.api import _setup
from capyidx.utils.disk_operations import DiskOperations
from capyidx.utils.uri1 import get_uri_to_path
from capyidx.watcher.file_watcher import PollingFileWatcher


def local_path(value: str) -> Path:
    if value.startswith("file://"):
        return Path(get_uri_to_path(value))
    return Path(unquote(urlparse(value).path)) if "://" in value else Path(value)


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    interval: float = 0.05,
    description: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {description}")
        await asyncio.sleep(interval)


def phase(number: int, message: str) -> None:
    print(f"[phase {number}] {message}")


def choose_symbol(conn, requested_name: str | None) -> Any:
    if requested_name:
        row = conn.execute(
            """
            SELECT s.id, s.name, s.path, s.startLine, s.endLine
            FROM symbols s
            WHERE s.name = ?
              AND EXISTS (SELECT 1 FROM chunks c WHERE c.symbolId = s.id)
            ORDER BY s.path, s.startLine, s.id
            LIMIT 1
            """,
            (requested_name,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT s.id, s.name, s.path, s.startLine, s.endLine
            FROM symbols s
            WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.symbolId = s.id)
            ORDER BY LENGTH(s.name) DESC, s.path, s.startLine, s.id
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        raise RuntimeError(
            "No chunk-backed symbols were found. The repository may contain "
            "no supported source files, or indexing failed."
        )
    return row


async def run(repo: Path, symbol_name: str | None, flush_interval: float) -> None:
    repo = repo.resolve()
    if not repo.is_dir():
        raise NotADirectoryError(repo)
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a Git repository")

    fs = DiskOperations(roots=[str(repo)])
    roots = await fs.get_workspace_dirs()
    _, _, tags = await _setup(repo)
    conn = open_index(tags)

    watch_task: asyncio.Task[None] | None = None
    indexer: CodeIndexer | None = None
    original_bytes: bytes | None = None
    edited_file: Path | None = None

    try:
        chunk_index = ChunkCodebaseIndex(
            db=conn,
            filesystem=fs,
            max_chunk_size=512,
        )
        indexer = CodeIndexer(
            fs=fs,
            indexes=[chunk_index],
            watcher=PollingFileWatcher(poll_interval=0.1),
            cache_max_bytes=2_000_000,
        )
        lookup = SymbolLookup(conn, roots)
        indexer.symbol_lookup = lookup  # type: ignore[attr-defined]

        phase(0, "checking instance state")
        assert indexer.system_ready is False
        assert indexer.pending_paths == frozenset()
        assert isinstance(indexer.get_index_status(), dict)

        phase(1, "checking lifecycle messages and readiness gate")
        messages = mcp_handshake()
        assert [item["method"] for item in messages] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        assert "get_index_status" not in MCP_TOOLS
        blocked = await handle_symbol_lookup(indexer, symbol_name or "anything", lookup)
        assert blocked["code"] == "INDEX_UNAVAILABLE"

        watch_updates: list[IndexingProgressUpdate] = []

        async def consume_watch() -> None:
            while True:
                async for update in indexer.start_watch(
                    db=conn,
                    workspace_dirs=roots,
                    flush_interval=flush_interval,
                ):
                    watch_updates.append(update)
                if not await indexer.wait_for_watch_restart():
                    return

        watch_task = asyncio.create_task(consume_watch())
        await wait_until(
            lambda: indexer is not None and indexer._watch_started is not None,
            timeout=5,
            description="watcher startup",
        )
        assert indexer._watch_started is not None
        await indexer._watch_started.wait()

        phase(1, "running the real initial index pass")
        initial_updates: list[IndexingProgressUpdate] = []
        async for update in indexer.refresh_codebase_index(roots, conn):
            initial_updates.append(update)
            print(f"  initial: {update.status} {update.progress:.0%} {update.desc}")
        if indexer.current_indexing_state.status != "done":
            raise AssertionError(
                f"initial indexing did not finish successfully: "
                f"{indexer.current_indexing_state}"
            )
        indexer.set_system_ready(True)

        row = choose_symbol(conn, symbol_name)
        symbol_id = str(row["id"])
        symbol_path = local_path(str(row["path"]))
        print(
            f"  selected symbol={row['name']!r} id={symbol_id!r} "
            f"path={symbol_path}"
        )

        phase(3, "warming the real symbol cache")
        warm_response = await handle_get_symbol(
            indexer,
            symbol_id,
            str(symbol_path),
            lookup,
            char_limit=2_000_000,
        )
        assert warm_response["id"] == symbol_id
        assert indexer._cache.get(symbol_id) is not None

        phase(2, "checking the real watcher and per-path pending state")
        original_bytes = symbol_path.read_bytes()
        edited_file = symbol_path
        suffix = symbol_path.suffix.lower()
        comment_text = (
            "# capyidx temporary watcher test"
            if suffix in {".py", ".rb", ".sh", ".yaml", ".yml"}
            else "// capyidx temporary watcher test"
        )
        comment = (comment_text + chr(10)).encode()
        symbol_path.write_bytes(original_bytes + bytes([10]) + comment)

        target = canonical_path(str(symbol_path))
        await wait_until(
            lambda: any(canonical_path(path) == target for path in indexer.pending_paths),
            timeout=5,
            description="edited file to become pending",
        )
        print("  pending paths:", sorted(indexer.pending_paths))
        assert any(canonical_path(path) == target for path in indexer.pending_paths)

        phase(3, "checking eager cache invalidation")
        assert indexer._cache.get(symbol_id) is None

        phase(4, "checking path-specific readiness waits")
        assert await wait_for_path_ready(
            indexer,
            str(symbol_path),
            max_wait_s=0.02,
            poll_s=0.005,
        ) is False
        assert await wait_for_path_ready(
            indexer,
            str(repo / "unrelated-file-that-does-not-exist"),
            max_wait_s=0.02,
            poll_s=0.005,
        ) is True

        await wait_until(
            lambda: all(canonical_path(path) != target for path in indexer.pending_paths),
            timeout=10,
            description="edited file refresh to finish",
        )

        phase(5, "checking get_symbol and get_symbol_range")
        fresh_row = conn.execute(
            """
            SELECT s.id, s.name, s.path, s.startLine, s.endLine
            FROM symbols s
            WHERE s.path = ? AND s.name = ?
              AND EXISTS (SELECT 1 FROM chunks c WHERE c.symbolId = s.id)
            ORDER BY s.startLine, s.id
            LIMIT 1
            """,
            (str(symbol_path), str(row["name"])),
        ).fetchone()
        if fresh_row is None:
            raise AssertionError("edited file was not present after refresh")

        if str(fresh_row["id"]) != symbol_id:
            old_response = await handle_get_symbol(
                indexer,
                symbol_id,
                str(symbol_path),
                lookup,
                char_limit=2_000_000,
            )
            assert old_response["code"] == "SYMBOL_NOT_FOUND"
            print(
                "  symbol id changed after content refresh:",
                symbol_id,
                "->",
                fresh_row["id"],
            )
            symbol_id = str(fresh_row["id"])
            row = fresh_row

        full_response = await handle_get_symbol(
            indexer,
            symbol_id,
            str(symbol_path),
            lookup,
            char_limit=2_000_000,
        )
        assert full_response["id"] == symbol_id
        assert "code" in full_response
        range_response = await handle_get_symbol_range(
            indexer,
            symbol_id,
            int(row["startLine"]),
            min(int(row["endLine"]), int(row["startLine"]) + 2),
            str(symbol_path),
            lookup,
        )
        assert range_response["start_line"] <= range_response["end_line"] # type: ignore
        assert "code" in range_response

        phase(8, "checking character truncation metadata")
        trunc_response = await handle_get_symbol(
            indexer,
            symbol_id,
            str(symbol_path),
            lookup,
            char_limit=1,
        )
        assert trunc_response["truncated"] is True
        assert "returned_end_line" in trunc_response
        assert "total_lines" in trunc_response

        phase(3, "checking cache hit after refresh")
        assert indexer._cache.get(symbol_id) is not None
        cached_status = indexer.get_index_status()
        print("  status after refresh:", json.dumps(cached_status, indent=2, default=list))

        phase(6, "checking branch reindex lifecycle without changing Git")
        branch_progress: list[IndexingProgressUpdate] = []

        async def progress_callback(update: IndexingProgressUpdate) -> None:
            branch_progress.append(update)
            print(f"  branch: {update.status} {update.progress:.0%} {update.desc}")

        await indexer._handle_branch_switch(
            roots[0],
            conn,
            workspace_dirs=roots,
            progress_callback=progress_callback,
        )
        assert indexer.system_ready is True
        assert branch_progress
        await wait_until(
            lambda: indexer._watch_started is not None,
            timeout=5,
            description="watcher restart after branch reindex",
        )

        phase(6, "checking isolated .git/HEAD polling")
        head_seen = asyncio.Event()
        original_branch_handler = indexer._handle_branch_switch

        async def probe_branch_handler(
            _workspace_dir: str,
            _db: Any,
            workspace_dirs: Any = None,
            progress_callback: Any = None,
        ) -> None:
            head_seen.set()

        with tempfile.TemporaryDirectory(prefix="capyidx-head-test-") as temp_name:
            probe_root = Path(temp_name)
            (probe_root / ".git").mkdir()
            head_file = probe_root / ".git" / "HEAD"
            head_file.write_text("ref: refs/heads/main\n", encoding="utf-8")
            indexer._handle_branch_switch = probe_branch_handler  # type: ignore[method-assign]
            head_task = asyncio.create_task(
                indexer.watch_git_head(
                    str(probe_root),
                    conn,
                    workspace_dirs=roots,
                    interval=0.02,
                )
            )
            await asyncio.sleep(0.05)
            head_file.write_text("ref: refs/heads/feature\n", encoding="utf-8")
            await asyncio.wait_for(head_seen.wait(), timeout=2)
            head_task.cancel()
            await asyncio.gather(head_task, return_exceptions=True)

        indexer._handle_branch_switch = original_branch_handler  # type: ignore[method-assign]

        phase(7, "checking host progress delivery")
        assert any(update.status in {"loading", "indexing", "done"} for update in branch_progress)

        phase(9, "checking watcher failure recovery signal")

        class FailingWatcher:
            async def watch(self, _roots):
                raise RuntimeError("intentional watcher failure")
                yield []

        failing_indexer = CodeIndexer(fs=fs, watcher=FailingWatcher()) #type: ignore

        async def consume_failure() -> bool:
            async for _ in failing_indexer.start_watch(
                db=conn,
                workspace_dirs=roots,
                flush_interval=60,
            ):
                pass
            return await failing_indexer.wait_for_watch_restart()

        assert await asyncio.wait_for(consume_failure(), timeout=3) is True

        phase(10, "checking diagnostic status snapshot")
        status = indexer.get_index_status()
        assert isinstance(status["pending_paths"], tuple)
        assert isinstance(status["in_flight_paths"], tuple)
        assert "watcher" in status
        assert "cache" in status
        print()
        print("PASS: phases 0-10 runtime behavior was exercised.")

    finally:
        if edited_file is not None and original_bytes is not None:
            edited_file.write_bytes(original_bytes)
        if indexer is not None:
            await indexer.stop_watch()
        if watch_task is not None:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument(
        "--symbol",
        help="symbol name to exercise; defaults to a chunk-backed symbol from the index",
    )
    parser.add_argument(
        "--flush-interval",
        type=float,
        default=0.5,
        help="incremental watcher flush interval in seconds (default: 0.5)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.repository, args.symbol, args.flush_interval))


if __name__ == "__main__":
    main()

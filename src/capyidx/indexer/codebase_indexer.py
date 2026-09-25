"""
Core codebase indexer orchestrator.
"""

from __future__ import annotations

import logging
import asyncio
import inspect
import re
import sqlite3
import time
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    List,
    Optional,
    Sequence,
)
from urllib.parse import unquote, urlparse

from capyidx.base.index_d import (
    FileSystem,
    FileStatsMap,
    IndexingProgressUpdate,
    IndexTag,
)
from capyidx.base.index_types import (
    CodebaseIndexer,
    IndexResultType,
    PathAndCacheKey,
    RefreshIndexResults,
    IndexContext
)
from capyidx.base.refresh_index import IndexLock, get_compute_delete_add_remove
from capyidx.walker.walk_dir import WalkerOptions, walk_dir_async
from capyidx.watcher.file_watcher import FileWatcher, AutoFileWatcher
from capyidx.utils.uri1 import get_uri_path_basename, get_uri_to_path
from capyidx.utils.disk_operations import DiskOperations
from capyidx.mcp.cache import SymbolCache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILES_PER_BATCH = 200

# SQLite / storage errors that warrant wiping indexes (mirrors TS regexes).
_ERRORS_TO_CLEAR_INDEXES_ON = [
    re.compile(
        r"Invalid argument error: Values length \d+ is less than the length "
        r"\(\d+\) multiplied by the value size \d+",
        re.I,
    ),
    re.compile(r"SQLITE_CONSTRAINT", re.I),
    re.compile(r"SQLITE_ERROR", re.I),
    re.compile(r"SQLITE_CORRUPT", re.I),
    re.compile(r"SQLITE_IOERR", re.I),
    re.compile(r"SQLITE_FULL", re.I),
]


# ---------------------------------------------------------------------------
# Pause / cancellation
# ---------------------------------------------------------------------------

class PauseToken:
    """pause flag """

    def __init__(self, paused: bool = False) -> None:
        self._paused = paused

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = value


class CancellationToken:
    """abort signal for long-running index runs."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def throw_if_cancelled(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError("Indexing cancelled")


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------

def _find_uri_in_dirs(file_uri: str, workspace_dirs: Sequence[str]) -> Optional[str]:
    """
    Return the workspace directory URI that contains *file_uri*, or None.
    Both sides are compared as normalised path strings (no scheme handling
    beyond stripping a leading file:// for prefix checks).
    """
    def _norm(u: str) -> str:
        if u.startswith("file://"):
            p = unquote(urlparse(u).path)
        else:
            p = u
        return p.rstrip("/")

    file_norm = _norm(file_uri)
    for d in workspace_dirs:
        d_norm = _norm(d)
        if file_norm == d_norm or file_norm.startswith(d_norm + "/"):
            return d
    return None


# ---------------------------------------------------------------------------
# CodeIndexer
# ---------------------------------------------------------------------------

class CodeIndexer:
    """
    codebase indexing orchestrator.

    Parameters
    ----------
    fs :
        Filesystem backend (typically a DiskOperations instance).
    indexes :
        Optional pre-built list of CodebaseIndexer backends. When provided,
        ``index_types`` is ignored and these indexes are used directly.
    index_types :
        Set of ContextIndexingType values to build via the internal
        factories. Factories are stubs until the concrete backends are
        ported; they currently return None.
    files_per_batch :
        Max files processed per ``CodebaseIndexer.update`` call (memory /
        embedding-request control). Default 200.
    initial_paused :
        Start in the paused state.
    disabled :
        If True, refresh_dirs immediately yields status="disabled".
    """

    def __init__(
        self,
        fs: DiskOperations,
        indexes: Optional[List[CodebaseIndexer]] = None,
        watcher: FileWatcher | None = None,
        files_per_batch: int = FILES_PER_BATCH,
        initial_paused: bool = False,
        disabled: bool = False,
        cache_max_bytes: int = 3072,
    ) -> None:
        
        self.logger = logging.getLogger(__name__)
        self.fs = fs
        self.watcher = watcher or AutoFileWatcher()
        self.files_per_batch = files_per_batch
        self.disabled = disabled

        self._pause = PauseToken(initial_paused)
        self._built_indexes: List[CodebaseIndexer] = list(indexes) if indexes else []
        
        self._directory_token: Optional[CancellationToken] = None
        self._file_token: Optional[CancellationToken] = None

        self._state: IndexingProgressUpdate = IndexingProgressUpdate(
            progress=0.0,
            desc="loading",
            status="loading",
        )

        self._system_ready: bool = False       # False during startup AND branch reindex
        self._pending: set[str] = set()         # queued, watcher saw it, refresh not started
        self._in_flight: set[str] = set()       # refresh actively running on these paths
        self._cache = SymbolCache(max_bytes=cache_max_bytes)  # cache for get_symbol hit
        self._watch_started: asyncio.Event | None = None
        self._watch_stop: asyncio.Event | None = None
        self._watch_stopped: asyncio.Event | None = None
        self._watch_fill_task: asyncio.Task[None] | None = None
        self._watch_restart: asyncio.Event | None = None
        self._watcher_error: BaseException | None = None
        self._branch_switch_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public pause / state
    # ------------------------------------------------------------------

    # used for inderxer start/restart
    @property
    def system_ready(self) -> bool:
        return self._system_ready

    # used for per-file indexing status check
    @property
    def pending_paths(self) -> frozenset[str]:
        return frozenset(self._pending | self._in_flight)

    @property
    def paused(self) -> bool:
        return self._pause.paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._pause.paused = value

    @property
    def current_indexing_state(self) -> IndexingProgressUpdate:
        return self._state

    def get_index_status(self) -> dict[str, object]:
        """Return a read-only diagnostic snapshot for internal host use.

        This is intentionally a normal indexer method, not an MCP tool.
        Collection values are copied to immutable tuples so callers cannot
        mutate the indexer's pending/in-flight state.
        """
        state = self._state
        pending = tuple(sorted(self._pending))
        in_flight = tuple(sorted(self._in_flight))
        unsettled = tuple(sorted(set(pending) | set(in_flight)))

        watcher_running = (
            self._watch_started is not None
            and self._watch_fill_task is not None
            and not self._watch_fill_task.done()
            and self._watch_stop is not None
            and not self._watch_stop.is_set()
        )
        watcher_state = (
            "failed"
            if self._watcher_error is not None
            else "running"
            if watcher_running
            else "stopped"
        )

        return {
            "system_ready": self._system_ready,
            "current": {
                "progress": state.progress,
                "description": state.desc,
                "status": state.status,
                "warnings": list(state.warnings or ()),
            },
            "pending_paths": pending,
            "in_flight_paths": in_flight,
            "unsettled_paths": unsettled,
            "pending_count": len(pending),
            "in_flight_count": len(in_flight),
            "watcher": {
                "state": watcher_state,
                "error": str(self._watcher_error)
                if self._watcher_error is not None
                else None,
            },
            "cache": {
                "current_bytes": self._cache.current_bytes,
                "max_bytes": self._cache.max_bytes,
            },
        }

    def cancel(self) -> None:
        """Cancel the in-flight full-directory refresh (if any)."""
        if self._directory_token:
            self._directory_token.cancel()

        if self._file_token:
            self._file_token.cancel()

    def set_system_ready(self, ready: bool) -> None:
        """Update the process-wide readiness gate used by MCP handlers."""
        self._system_ready = ready

    def _on_watcher_died(self, task: asyncio.Task[object]) -> None:
        """Make an unexpected watcher failure visible instead of silent."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._watcher_error = error
            self.logger.error(
                "file watcher stopped unexpectedly; requesting watcher restart",
                exc_info=(type(error), error, error.__traceback__),
            )
            if self._watch_stop is not None:
                self._watch_stop.set()

    async def start_watch(
        self,
        db: sqlite3.Connection,
        workspace_dirs: list[str],
        flush_interval: float = 10.0,
    ):
        """
        Consume the file watcher, batching changes until each flush interval.
        The watch can be stopped safely by a branch-switch supervisor.
        """
        watcher = self.watcher.watch(workspace_dirs)
        self._watch_started = asyncio.Event()
        self._watch_stop = asyncio.Event()
        self._watcher_error = None
        self._watch_stopped = asyncio.Event()
        if self._watch_restart is None:
            self._watch_restart = asyncio.Event()

        async def _fill() -> None:
            assert self._watch_started is not None
            self._watch_started.set()
            async for batch in watcher:
                for path in batch:
                    self._pending.add(path)
                    self._cache.invalidate_path(path)

        fill_task = asyncio.create_task(_fill())
        self._watch_fill_task = fill_task
        fill_task.add_done_callback(self._on_watcher_died)

        try:
            while self._watch_stop is not None and not self._watch_stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._watch_stop.wait(),
                        timeout=flush_interval,
                    )
                except asyncio.TimeoutError:
                    pass

                if self._watch_stop.is_set():
                    break
                if not self._pending:
                    continue

                # Preserve watcher events until the initial/full refresh ends.
                if (
                    self._directory_token is not None
                    and not self._directory_token.cancelled
                ):
                    continue

                batch = set(self._pending)
                self._pending.difference_update(batch)
                self._in_flight.update(batch)

                batch_failed = False
                try:
                    async for update in self.refresh_codebase_index_files(
                        files=sorted(batch),
                        db=db,
                    ):
                        yield update
                        if update.status in {"failed", "cancelled"}:
                            batch_failed = True
                    if batch_failed:
                        raise RuntimeError("incremental refresh reported a failure")
                except Exception:
                    self.logger.exception("refresh failed for batch, requeueing")
                    self._pending.update(batch)
                finally:
                    self._in_flight.difference_update(batch)
        finally:
            fill_task.cancel()
            try:
                await fill_task
            except asyncio.CancelledError:
                pass
            except Exception:
                self.logger.exception("file watcher task terminated with an error")
            self._watch_fill_task = None
            if self._watch_stopped is not None:
                self._watch_stopped.set()
            self._watch_started = None

    async def stop_watch(self) -> None:
        """Stop the active file watcher and wait for its refresh loop."""
        stop = self._watch_stop
        stopped = self._watch_stopped
        if stop is None or stopped is None:
            return
        stop.set()
        if self._watch_fill_task is not None:
            self._watch_fill_task.cancel()
        await stopped.wait()
        self._watch_stop = None
        self._watch_stopped = None

    async def wait_for_watch_restart(self) -> bool:
        """Wait for a branch switch or watcher failure before restarting."""
        while True:
            if self._watch_restart is not None and self._watch_restart.is_set():
                self._watch_restart.clear()
                return True
            if self._watcher_error is not None:
                self._watcher_error = None
                await asyncio.sleep(0.25)
                return True
            await asyncio.sleep(0.1)

    async def _handle_branch_switch(
        self,
        workspace_dir: str,
        db: sqlite3.Connection,
        workspace_dirs: Sequence[str] | None = None,
        progress_callback: Any | None = None,
    ) -> None:
        async with self._branch_switch_lock:
            self._system_ready = False
            await self.stop_watch()
            self._pending.clear()
            self._in_flight.clear()
            self._cache.clear()

            dirs = list(workspace_dirs or await self.fs.get_workspace_dirs())
            async for update in self.refresh_codebase_index(dirs, db):
                if progress_callback is not None:
                    result = progress_callback(update)
                    if inspect.isawaitable(result):
                        await result

            if self.current_indexing_state.status == "done":
                self._system_ready = True
            if self._watch_restart is not None:
                self._watch_restart.set()

    async def watch_git_head(
        self,
        workspace_dir: str,
        db: sqlite3.Connection,
        workspace_dirs: Sequence[str] | None = None,
        progress_callback: Any | None = None,
        interval: float = 0.5,
    ) -> None:
        """Poll .git/HEAD and reindex atomically when its content changes."""
        root = (
            get_uri_to_path(workspace_dir)
            if workspace_dir.startswith("file://")
            else workspace_dir
        )
        head_path = Path(root) / ".git" / "HEAD"
        last = head_path.read_bytes() if head_path.exists() else None

        try:
            while True:
                await asyncio.sleep(interval)
                current = head_path.read_bytes() if head_path.exists() else None
                if current == last:
                    continue
                last = current
                await self._handle_branch_switch(
                    workspace_dir,
                    db,
                    workspace_dirs=workspace_dirs,
                    progress_callback=progress_callback,
                )
        except asyncio.CancelledError:
            return

    async def get_indexes_to_build(self) -> List[CodebaseIndexer]:
        """
        Return the list of CodebaseIndexer backends to run.
        """
        if self._built_indexes:
            return list(self._built_indexes)
        
        return []

    # ------------------------------------------------------------------
    # Friendly names for progress / warnings
    # ------------------------------------------------------------------

    @staticmethod
    def _friendly_index_name(artifact_id: str) -> str:
        mapping = {
            "fullTextSearch": "Full text search",
            "codeSnippets": "Code snippets",
            "chunks": "Chunking",
        }
        if artifact_id in mapping:
            return mapping[artifact_id]
        if artifact_id.startswith("vectordb") or artifact_id.startswith("embeddings"):
            return "Embedding"
        return artifact_id

    # ------------------------------------------------------------------
    # Batching helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _total_index_ops(results: RefreshIndexResults) -> int:
        return (
            len(results.compute)
            + len(results.delete)
            + len(results.add_tag)
            + len(results.remove_tag)
        )

    async def _batch_refresh_index_results(
    self,
    results: RefreshIndexResults,
    ):
        n = self.files_per_batch
        cur = 0

        while (
            cur < len(results.compute)
            or cur < len(results.delete)
            or cur < len(results.add_tag)
            or cur < len(results.remove_tag)
        ):
            yield RefreshIndexResults(
                compute=results.compute[cur:cur+n],
                delete=results.delete[cur:cur+n],
                add_tag=results.add_tag[cur:cur+n],
                remove_tag=results.remove_tag[cur:cur+n],
            )

            cur += n

    @staticmethod
    def _single_file_index_ops(
        results: RefreshIndexResults,
        last_updated: List[PathAndCacheKey],
        file_path: str,
    ) -> tuple[RefreshIndexResults, List[PathAndCacheKey]]:
        def _f(item: PathAndCacheKey) -> bool:
            return item.path == file_path

        return (
            RefreshIndexResults(
                compute=[i for i in results.compute if _f(i)],
                delete=[i for i in results.delete if _f(i)],
                add_tag=[i for i in results.add_tag if _f(i)],
                remove_tag=[i for i in results.remove_tag if _f(i)],
            ),
            [i for i in last_updated if _f(i)],
        )

    # ------------------------------------------------------------------
    # Pause helper
    # ------------------------------------------------------------------

    async def _yield_update_and_pause(
        self,
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        yield IndexingProgressUpdate(
            progress=0.0,
            desc="Indexing Paused",
            status="paused",
        )
        while self._pause.paused:
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Error : progress
    # ------------------------------------------------------------------

    def _error_to_progress_update(self, err: BaseException) -> IndexingProgressUpdate:
        err_msg = str(err)
        should_clear = any(r.search(err_msg) for r in _ERRORS_TO_CLEAR_INDEXES_ON)
        return IndexingProgressUpdate(
            progress=0.0,
            desc=err_msg,
            status="failed",
            should_clear_indexes=should_clear or None,
            debug_info=getattr(err, "__traceback__", None)
            and "".join(
                __import__("traceback").format_exception(
                    type(err), err, err.__traceback__
                )
            )[:2000]
            or None,
        )

    def _handle_error(self, err: BaseException) -> IndexingProgressUpdate:
        if isinstance(err, Exception):
            return self._error_to_progress_update(err)
        return IndexingProgressUpdate(
            progress=0.0,
            desc=f"Indexing failed: {err}",
            status="failed",
        )

    # ------------------------------------------------------------------
    # Cross-process lock wait
    # ------------------------------------------------------------------

    async def _wait_for_db_index(
        self,
        db:sqlite3.Connection
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        """
        Block while another process holds IndexLock.
        Stale locks (>10 s without timestamp refresh) are released.
        """
        found = IndexLock.is_locked(db)
        while found and found.get("locked"):
            age_s = (time.time() * 1000 - found["timestamp"]) / 1000.0
            if age_s > 10:
                IndexLock.unlock(db)
                break
            yield IndexingProgressUpdate(
                progress=0.0,
                desc=f"Waiting for lock held by: {found.get('dirs', '')}",
                status="waiting",
            )
            await asyncio.sleep(1.0)
            found = IndexLock.is_locked(db)

    # ------------------------------------------------------------------
    # Core: index a list of files for one directory against all backends
    # ------------------------------------------------------------------

    async def _index_files(
        self,
        directory: str,
        files: List[str],
        branch: str,
        db:sqlite3.Connection,
        repo_name: Optional[str],
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        
        # print("here3")
        indexes = await self.get_indexes_to_build()
        # print("here4")
        # print(indexes)
        if not indexes:
            weights = []
            
        weights = [
            max(index.relative_expected_time, 1e-4)
            for index in indexes
        ]
        total_weight = sum(weights)
        completed_weight = 0.0
        
        stats: FileStatsMap = await self.fs.get_file_stats(files)
        # print(stats.keys())
        # print("okay---- indexer -line 490")
        # indexes = await self.get_indexes_to_build()
        if not indexes:
            yield IndexingProgressUpdate(
                progress=1.0,
                desc="No indexes configured",
                status="done",
            )
            return

        # completed_index_count = 0
        progress = 0.0
        warnings: List[str] = []

        for index_idx, codebase_index in enumerate(indexes):
            tag = IndexTag(
                directory=directory,
                branch=branch,
                artifact_id=codebase_index.artifact_id,
            )
            yield IndexingProgressUpdate(
                progress=progress,
                desc=f"Planning changes for {codebase_index.artifact_id} index...",
                status="indexing",
                warnings=list(warnings) if warnings else None,
            )

            try:
                results, last_updated, mark_complete, mark_last_updated = await get_compute_delete_add_remove(
                    tag,
                    dict(stats),
                    db,
                    self.fs.read_file,
                    repo_name,
                )
                total_ops = self._total_index_ops(results)
                completed_ops = 0

                if total_ops > 0:
                    async for sub in self._batch_refresh_index_results(results):
                        try:
                            context = IndexContext(
                                filesystem=self.fs,
                                repo_name=repo_name,
                            )
                            async for update in codebase_index.update(
                                tag, context, sub, mark_complete
                            ):
                                yield IndexingProgressUpdate(
                                    progress=progress,
                                    desc=update.desc,
                                    status="indexing",
                                    warnings=list(warnings) if warnings else None,
                                )
                            completed_ops += (
                                len(sub.compute)
                                + len(sub.delete)
                                + len(sub.add_tag)
                                + len(sub.remove_tag)
                            )
                            
                            current_weight = weights[index_idx]

                            progress = (
                                completed_weight +
                                current_weight * (completed_ops / total_ops)
                            ) / total_weight
                            
                        except Exception as err:
                            friendly = self._friendly_index_name(
                                codebase_index.artifact_id
                            )
                            
                            msg = f"{friendly}: {err}"
                            warnings.append(msg)
                            completed_ops += (
                                len(sub.compute)
                                + len(sub.delete)
                                + len(sub.add_tag)
                                + len(sub.remove_tag)
                            )
                            
                            current_weight = weights[index_idx]
                            
                            progress = (
                                completed_weight +
                                current_weight * (completed_ops / total_ops)
                            ) / total_weight

                await mark_last_updated(
                    last_updated,
                    IndexResultType.UPDATE_LAST_UPDATED,
                )
                completed_weight += weights[index_idx]
                progress = completed_weight / total_weight

            except Exception as err:
                friendly = self._friendly_index_name(codebase_index.artifact_id)
                warnings.append(f"{friendly}: {err}")
                
                completed_weight += weights[index_idx]
                progress = completed_weight / total_weight

        if warnings:
            yield IndexingProgressUpdate(
                progress=1.0,
                desc=f"Indexing completed with {len(warnings)} warning(s)",
                status="done",
                warnings=list(warnings),
            )

    # ------------------------------------------------------------------
    # refresh whole directories
    # ------------------------------------------------------------------

    async def refresh_dirs(
        self,
        dirs: Sequence[str],
        db:sqlite3.Connection,
        cancellation: Optional[CancellationToken] = None,
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        """
        Walk each directory, plan compute/delete/addTag/removeTag, then
        run every configured CodebaseIndexer backend in batches.

        Yields IndexingProgressUpdate throughout. Honours pause and
        cancellation.
        """
        token = cancellation or CancellationToken()
        # self._active_cancellation = token

        progress = 0.0

        if not dirs:
            upd = IndexingProgressUpdate(
                progress=1.0, desc="Nothing to index", status="done"
            )
            self._state = upd
            yield upd
            return

        if self.disabled:
            upd = IndexingProgressUpdate(
                progress=progress, desc="Indexing is disabled", status="disabled"
            )
            self._state = upd
            yield upd
            return

        try:
            await self.fs.get_repo_name(dirs[0])
        except Exception:
            pass

        yield IndexingProgressUpdate(
            progress=progress, desc="Starting indexing...", status="loading"
        )

        collected_warnings: List[str] = []

        try:
            for directory in dirs:
                token.throw_if_cancelled()
                dir_basename = get_uri_path_basename(directory)
                yield IndexingProgressUpdate(
                    progress=progress,
                    desc=f"Discovering files in {dir_basename}...",
                    status="indexing",
                )

                directory_files: List[str] = []
                async for p in walk_dir_async(
                    directory,
                    self.fs,  # type: ignore[arg-type]
                    WalkerOptions(source="codebase indexing: refresh dirs"),
                ):
                    directory_files.append(p)
                    if token.cancelled:
                        upd = IndexingProgressUpdate(
                            progress=0.0,
                            desc="Indexing cancelled",
                            status="cancelled",
                        )
                        self._state = upd
                        yield upd
                        return
                    if self._pause.paused:
                        async for u in self._yield_update_and_pause():
                            self._state = u
                            yield u

                branch = await self.fs.get_branch(directory)
                repo_name = await self.fs.get_repo_name(directory)
                async for update in self._index_files(
                    directory, directory_files, branch, db, repo_name
                ):
                    if token.cancelled:
                        upd = IndexingProgressUpdate(
                            progress=0.0,
                            desc="Indexing cancelled",
                            status="cancelled",
                        )
                        self._state = upd
                        yield upd
                        return
                    if self._pause.paused:
                        async for u in self._yield_update_and_pause():
                            self._state = u
                            yield u

                    if update.warnings:
                        collected_warnings = list(update.warnings)

                    self._state = update
                    yield update

            final = IndexingProgressUpdate(
                progress=1.0,
                desc=(
                    f"Indexing completed with {len(collected_warnings)} warning(s)"
                    if collected_warnings
                    else "Indexing Complete"
                ),
                status="done",
                warnings=collected_warnings or None,
            )
            self._state = final
            yield final

        except asyncio.CancelledError:
            upd = IndexingProgressUpdate(
                progress=0.0, desc="Indexing cancelled", status="cancelled"
            )
            self._state = upd
            yield upd
        except Exception as err:
            upd = self._handle_error(err)
            self._state = upd
            yield upd
            
    # ------------------------------------------------------------------
    # single-file / multi-file refresh
    # ------------------------------------------------------------------

    async def refresh_file(
        self,
        file: str,
        db:sqlite3.Connection,
        workspace_dirs: Sequence[str],
        cancellation: Optional[CancellationToken] = None
    ) -> None:
        """
        Re-index one file against every configured backend.
        No-op when paused or when the file lies outside workspace_dirs.
        """
        token = cancellation or CancellationToken()
        token.throw_if_cancelled()              
        if self._pause.paused:
            return

        found_in_dir = _find_uri_in_dirs(file, workspace_dirs)
        if found_in_dir is None:
            return

        branch = await self.fs.get_branch(found_in_dir)
        repo_name = await self.fs.get_repo_name(found_in_dir)
        indexes = await self.get_indexes_to_build()
        stats = await self.fs.get_file_stats([file])

        if stats:
            file_path = next(iter(stats.keys()))
        else:
            file_path = get_uri_to_path(file)
            
        for index in indexes:
            token.throw_if_cancelled()
            tag = IndexTag(
                directory=found_in_dir,
                branch=branch,
                artifact_id=index.artifact_id,
            )
            only = {file_path}
            
            full_results, full_last_updated, mark_complete, mark_last_updated = (
                await get_compute_delete_add_remove(
                    tag,
                    dict(stats),
                    db,
                    self.fs.read_file,
                    repo_name,
                    only_paths=only,
                )
            )
            results, last_updated = self._single_file_index_ops(
                full_results, full_last_updated, file_path
            )
            if self._total_index_ops(results) + len(last_updated) == 0:
                continue
            
            context = IndexContext(
                filesystem=self.fs,
                repo_name=repo_name,
            )
            async for _ in index.update(tag, context, results, mark_complete):
                pass
            await mark_last_updated(
                last_updated, IndexResultType.UPDATE_LAST_UPDATED
            )

    async def refresh_files(
        self,
        files: Sequence[str],
        db:sqlite3.Connection,
        cancellation: Optional[CancellationToken] = None
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        
        """Re-index an explicit list of files, yielding progress."""
        
        token = cancellation or CancellationToken()
        
        if not files:
            yield IndexingProgressUpdate(
                progress=1.0, desc="Indexing Complete", status="done"
            )
            return

        workspace_dirs = await self.fs.get_workspace_dirs()
        progress = 0.0
        progress_per = 1.0 / len(files)

        try:
            for file in files:
                token.throw_if_cancelled()
                yield IndexingProgressUpdate(
                    progress=progress,
                    desc=f"Indexing file {file}...",
                    status="indexing",
                )
                await self.refresh_file(file, db, workspace_dirs, cancellation=token)
                progress += progress_per

                if self._pause.paused:
                    async for u in self._yield_update_and_pause():
                        yield u

            yield IndexingProgressUpdate(
                progress=1.0, desc="Indexing Complete", status="done"
            )
        except asyncio.CancelledError:
            return
        except Exception as err:
            yield self._handle_error(err)

    # ------------------------------------------------------------------
    # High-level entry points with lock
    # ------------------------------------------------------------------

    async def refresh_codebase_index(
        self,
        paths: Sequence[str],
        db: sqlite3.Connection,
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        """
        Full directory refresh with cross-process IndexLock.

        Cancels any previous run, waits for a stale/foreign lock, acquires
        the lock, runs refresh_dirs, then releases the lock.
        """
        # Cancel previous
        if self._directory_token:
            self._directory_token.cancel()

        token = CancellationToken()
        self._directory_token = token
        
        async for update in self._wait_for_db_index(db=db):
            self._state = update
            yield update

        IndexLock.lock(", ".join(paths), db=db)
        timestamp_task = asyncio.create_task(self._lock_heartbeat(db=db))

        try:
            # print(paths)
            async for update in self.refresh_dirs(paths, db=db, cancellation=token):
                self._state = update
                yield update
        except Exception as err:
            upd = self._handle_error(err)
            self._state = upd
            yield upd
        finally:
            timestamp_task.cancel()
            try:
                await timestamp_task
            except asyncio.CancelledError:
                pass
            IndexLock.unlock(db=db)
            if self._directory_token is token:
                self._directory_token = None

    async def _lock_heartbeat(self, db:sqlite3.Connection,) -> None:
        """Refresh IndexLock timestamp every 5 s while we hold it."""
        try:
            while True:
                await asyncio.sleep(5.0)
                IndexLock.update_timestamp(db=db)
        except asyncio.CancelledError:
            return

    async def refresh_codebase_index_files(
        self,
        db:sqlite3.Connection,
        files: Sequence[str],
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        """
        File-level refresh. Does not take IndexLock (directory refresh
        owns the lock). Skips if a full directory refresh is already running.
        """
        if (
            self._directory_token is not None
            and not self._directory_token.cancelled
        ):
            return

        if self._file_token:
            self._file_token.cancel()

        token = CancellationToken()
        self._file_token = token
        
        try:
            async for update in self.refresh_files(files, db=db, cancellation=token):
                # print("here")
                self._state = update
                yield update
        except asyncio.CancelledError:
            return
        except Exception as err:
            upd = self._handle_error(err)
            self._state = upd
            yield upd
        finally:
            if self._file_token is token:
                self._file_token = None

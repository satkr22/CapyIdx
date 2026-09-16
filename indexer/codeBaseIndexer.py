"""
Core codebase indexer orchestrator (generic Python package).

Ported from Continue's CodebaseIndexerer.ts — only the indexing control flow.
No Continue config, IDE messenger, ContinueServerClient, or embed-model
selection. Callers inject a FileSystem and either concrete CodebaseIndexer
instances or a set of ContextIndexingType values (factories are stubs until
the individual backends are ported).

Depends on the already-ported modules:
  - index_d          (types, FileSystem protocol, ContextIndexingType)
  - index_types      (CodebaseIndexer protocol, RefreshIndexResults, …)
  - refresh_index    (get_compute_delete_add_remove, IndexLock)
  - walk_dir         (walk_dir_async, WalkerOptions)
  - disk_operations  (DiskOperations – typical FileSystem impl)
  - db               (SqliteDB – initialised by the caller before use)
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import (
    AsyncGenerator,
    # Callable,
    List,
    Optional,
    Sequence,
    # Set,
)
from urllib.parse import unquote, urlparse

from base.index_d import (
    # ContextIndexingType,
    FileSystem,
    FileStatsMap,
    IndexingProgressUpdate,
    IndexTag,
)
from base.index_types import (
    CodebaseIndexer,
    IndexResultType,
    PathAndCacheKey,
    RefreshIndexResults,
    IndexContext
)
from base.refresh_index import IndexLock, get_compute_delete_add_remove
from walker.walk_dir import WalkerOptions, walk_dir_async
from watcher.file_watcher import FileWatcher, AutoFileWatcher

# from dataclasses import dataclass


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
    """Simple mutable pause flag (mirrors TS PauseToken)."""

    def __init__(self, paused: bool = False) -> None:
        self._paused = paused

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = value


class CancellationToken:
    """Lightweight abort signal for long-running index runs."""

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
# URI helpers (minimal replacements for Continue util/uri)
# ---------------------------------------------------------------------------

def _uri_path_basename(uri: str) -> str:
    """Basename of a file:// URI or plain path."""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path)
    else:
        path = uri
    return Path(path.rstrip("/")).name or path


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
    Generic codebase indexing orchestrator.

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
        fs: FileSystem,
        indexes: Optional[List[CodebaseIndexer]] = None,
        watcher: FileWatcher | None = None,
        # index_types: Optional[Set[ContextIndexingType]] = None,
        files_per_batch: int = FILES_PER_BATCH,
        initial_paused: bool = False,
        disabled: bool = False,
    ) -> None:
        self.fs = fs
        self.watcher = watcher or AutoFileWatcher()
        self.files_per_batch = files_per_batch
        self.disabled = disabled

        self._pause = PauseToken(initial_paused)
        self._built_indexes: List[CodebaseIndexer] = list(indexes) if indexes else []
        # self._index_types: Set[ContextIndexingType] = set(index_types or [])
        
        self._directory_token: Optional[CancellationToken] = None
        self._file_token: Optional[CancellationToken] = None

        self._state: IndexingProgressUpdate = IndexingProgressUpdate(
            progress=0.0,
            desc="loading",
            status="loading",
        )

    # ------------------------------------------------------------------
    # Public pause / state
    # ------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._pause.paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._pause.paused = value

    @property
    def current_indexing_state(self) -> IndexingProgressUpdate:
        return self._state

    def cancel(self) -> None:
        """Cancel the in-flight full-directory refresh (if any)."""
        if self._directory_token:
            self._directory_token.cancel()

        if self._file_token:
            self._file_token.cancel()

    # ------------------------------------------------------------------
    # Index construction (getIndexesToBuild equivalent)
    # ------------------------------------------------------------------
    async def start_watch(
    self,
    workspace_dirs: list[str],
    ):
        watcher = self.watcher.watch(workspace_dirs)
        async for changed_files in watcher:
            async for update in self.refresh_codebase_index_files(changed_files):
                yield update

    async def get_indexes_to_build(self) -> List[CodebaseIndexer]:
        """
        Return the list of CodebaseIndexer backends to run.
        """
        if self._built_indexes:
            return list(self._built_indexes)
        
        return []

        # if not self._index_types:
        #     return []

        # factories: dict[ContextIndexingType, Callable[[], object]] = {
        #     "chunk": self._make_chunk_index,
        #     "code_snippets": self._make_snippets_index,
        #     "full_text_search": self._make_fts_index,
        #     "embeddings": self._make_embeddings_index,
        # }

        # indexes: List[CodebaseIndexer] = []
        # # Sequential – avoids concurrent SQLite setup races once backends exist.
        # for index_type in self._index_types:
        #     factory = factories.get(index_type)
        #     if factory is None:
        #         continue
        #     index = await factory()  # type: ignore[misc]
        #     if index is not None:
        #         indexes.append(index)

        # self._built_indexes = indexes
        # return list(indexes)

    # def set_indexes(self, indexes: List[CodebaseIndexer]) -> None:
    #     """Replace the active index list (e.g. after backends are ready)."""
    #     self._built_indexes = list(indexes)

    # def set_index_types(self, types: Set[ContextIndexingType]) -> None:
    #     """
    #     Change the requested types and clear the built cache so the next
    #     get_indexes_to_build() rebuilds.
    #     """
    #     self._index_types = set(types)
    #     self._built_indexes = []




    # # --- factory stubs (fill in when porting each backend) ---------------

    # async def _make_chunk_index(self) -> Optional[CodebaseIndexer]:
    #     """
    #     TODO: port ChunkCodebaseIndexer from Continue.
    #     Needs: read_file callable, max embedding chunk size (optional).
    #     """
    #     return None

    # async def _make_snippets_index(self) -> Optional[CodebaseIndexer]:
    #     """
    #     TODO: port CodeSnippetsCodebaseIndexer from Continue.
    #     Needs: FileSystem (or IDE-like) for reading / AST if required.
    #     """
    #     return None

    # async def _make_fts_index(self) -> Optional[CodebaseIndexer]:
    #     """
    #     TODO: port FullTextSearchCodebaseIndexer from Continue.
    #     Pure SQLite FTS; no external model required.
    #     """
    #     return None

    # async def _make_embeddings_index(self) -> Optional[CodebaseIndexer]:
    #     """
    #     TODO: port LanceDbIndex (or equivalent vector index) from Continue.
    #     Needs: embeddings provider + read_file callable.
    #     """
    #     return None

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
    # Error → progress
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
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        """
        Block while another process holds IndexLock.
        Stale locks (>10 s without timestamp refresh) are released.
        """
        found = IndexLock.is_locked()
        while found and found.get("locked"):
            age_s = (time.time() * 1000 - found["timestamp"]) / 1000.0
            if age_s > 10:
                IndexLock.unlock()
                break
            yield IndexingProgressUpdate(
                progress=0.0,
                desc=f"Waiting for lock held by: {found.get('dirs', '')}",
                status="waiting",
            )
            await asyncio.sleep(1.0)
            found = IndexLock.is_locked()

    # ------------------------------------------------------------------
    # Core: index a list of files for one directory against all backends
    # ------------------------------------------------------------------

    async def _index_files(
        self,
        directory: str,
        files: List[str],
        branch: str,
        repo_name: Optional[str],
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        
        indexes = await self.get_indexes_to_build()
        if not indexes:
            weights = []
            
        weights = [
            max(index.relative_expected_time, 1e-4)
            for index in indexes
        ]
        total_weight = sum(weights)
        completed_weight = 0.0
        
        stats: FileStatsMap = await self.fs.get_file_stats(files)
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
                            
                            # progress = (
                            #     (completed_index_count + completed_ops / total_ops)
                            #     * (1.0 / len(indexes))
                            # )
                            
                            current_weight = weights[index_idx]

                            progress = (
                                completed_weight +
                                current_weight * (completed_ops / total_ops)
                            ) / total_weight
                            
                        except Exception as err:
                            # Non-fatal per-batch: record warning and continue.
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
                # completed_index_count += 1
                # progress = completed_index_count * (1.0 / len(indexes))
                completed_weight += weights[index_idx]
                progress = completed_weight / total_weight

            except Exception as err:
                friendly = self._friendly_index_name(codebase_index.artifact_id)
                warnings.append(f"{friendly}: {err}")
                # completed_index_count += 1
                # progress = completed_index_count * (1.0 / len(indexes))
                
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
    # Public: refresh whole directories
    # ------------------------------------------------------------------

    async def refresh_dirs(
        self,
        dirs: Sequence[str],
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

        yield IndexingProgressUpdate(
            progress=progress, desc="Starting indexing", status="loading"
        )

        # Touch git early so we don't sit at 0 % waiting for it later.
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

                dir_basename = _uri_path_basename(directory)
                yield IndexingProgressUpdate(
                    progress=progress,
                    desc=f"Discovering files in {dir_basename}...",
                    status="indexing",
                )

                directory_files: List[str] = []
                # walk_dir currently typed against DiskOperations; FileSystem
                # implementations that match the protocol work at runtime.
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
                    directory, directory_files, branch, repo_name
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
        # finally:
        #     if self._active_cancellation is token:
        #         self._active_cancellation = None

    # ------------------------------------------------------------------
    # Public: single-file / multi-file refresh
    # ------------------------------------------------------------------

    async def refresh_file(
        self,
        file: str,
        workspace_dirs: Sequence[str],
    ) -> None:
        """
        Re-index one file against every configured backend.
        No-op when paused or when the file lies outside workspace_dirs.
        """
        if self._pause.paused:
            return

        found_in_dir = _find_uri_in_dirs(file, workspace_dirs)
        if found_in_dir is None:
            return

        branch = await self.fs.get_branch(found_in_dir)
        repo_name = await self.fs.get_repo_name(found_in_dir)
        indexes = await self.get_indexes_to_build()
        stats = await self.fs.get_file_stats([file])
        if not stats:
            return
        file_path = next(iter(stats.keys()))

        for index in indexes:
            tag = IndexTag(
                directory=found_in_dir,
                branch=branch,
                artifact_id=index.artifact_id,
            )
            full_results, full_last_updated, mark_complete, mark_last_updated = (
                await get_compute_delete_add_remove(
                    tag,
                    dict(stats),
                    self.fs.read_file,
                    repo_name,
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
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        """Re-index an explicit list of files, yielding progress."""
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
                yield IndexingProgressUpdate(
                    progress=progress,
                    desc=f"Indexing file {file}...",
                    status="indexing",
                )
                await self.refresh_file(file, workspace_dirs)
                progress += progress_per

                if self._pause.paused:
                    async for u in self._yield_update_and_pause():
                        yield u

            yield IndexingProgressUpdate(
                progress=1.0, desc="Indexing Complete", status="done"
            )
        except Exception as err:
            yield self._handle_error(err)

    # ------------------------------------------------------------------
    # High-level entry points with lock (mirrors refreshCodebaseIndexer)
    # ------------------------------------------------------------------

    async def refresh_codebase_index(
        self,
        paths: Sequence[str],
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
        
        async for update in self._wait_for_db_index():
            self._state = update
            yield update

        IndexLock.lock(", ".join(paths))
        timestamp_task = asyncio.create_task(self._lock_heartbeat())

        try:
            async for update in self.refresh_dirs(paths, cancellation=token):
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
            IndexLock.unlock()
            if self._directory_token is token:
                self._directory_token = None

    async def _lock_heartbeat(self) -> None:
        """Refresh IndexLock timestamp every 5 s while we hold it."""
        try:
            while True:
                await asyncio.sleep(5.0)
                IndexLock.update_timestamp()
        except asyncio.CancelledError:
            return

    async def refresh_codebase_index_files(
        self,
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
            async for update in self.refresh_files(files):
                self._state = update
                yield update
        except Exception as err:
            upd = self._handle_error(err)
            self._state = upd
            yield upd
        finally:
            if self._file_token is token:
                self._file_token = None

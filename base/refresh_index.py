"""Refresh / catalog logic for the codebase indexer.
tag_catalog + global_cache + four-list ops + IndexLock.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

from base.index_types import (
    CodebaseIndexer,
    IndexingProgressUpdate,
    IndexResultType,
    IndexTag,
    MarkCompleteCallback,
    PathAndCacheKey,
    RefreshIndexResults,
)
from base.index_d import FileStatsMap

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
SQLITE_MAX_LIKE_PATTERN_LENGTH = 50_000
_READ_CONCURRENCY = 10


from base.db import SqliteDB

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def calculate_hash(file_contents: str) -> str:
    return hashlib.sha256(file_contents.encode("utf-8")).hexdigest()


def truncate_to_last_n_bytes(input_str: str, max_bytes: int) -> str:
    encoded = input_str.encode("utf-8")
    if len(encoded) <= max_bytes:
        return input_str
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def truncate_sqlite_like_pattern(input_str: str, safety: int = 100) -> str:
    return truncate_to_last_n_bytes(
        input_str, SQLITE_MAX_LIKE_PATTERN_LENGTH - safety
    )


class AddRemoveResultType(str, Enum):
    ADD = "add"
    REMOVE = "remove"
    UPDATE_NEW_VERSION = "updateNewVersion"
    UPDATE_OLD_VERSION = "updateOldVersion"
    UPDATE_LAST_UPDATED = "updateLastUpdated"
    COMPUTE = "compute"


def _map_result_type(result_type: IndexResultType) -> AddRemoveResultType:
    if result_type == IndexResultType.UPDATE_LAST_UPDATED:
        return AddRemoveResultType.UPDATE_LAST_UPDATED
    if result_type == IndexResultType.COMPUTE:
        return AddRemoveResultType.COMPUTE
    if result_type == IndexResultType.ADD_TAG:
        return AddRemoveResultType.ADD
    if result_type in (IndexResultType.DELETE, IndexResultType.REMOVE_TAG):
        return AddRemoveResultType.REMOVE
    raise ValueError(f"Unexpected result type: {result_type}")


def get_saved_items_for_tag(
    tag: IndexTag,
    only_paths: Optional[set[str]] = None,
) -> list[tuple[str, str, int]]:
    """Return (path, cache_key, last_updated) rows for a tag.
    
    When only_paths is given, only those paths are returned (O(1) for
    single-file refresh). Otherwise returns the full catalog for the tag.
    """
    db = SqliteDB.get()
    if only_paths is not None:
        # SQLite has a practical limit on the number of variables 999 by
        # default). Single-file refresh always has |only_paths| == 1, so
        # this is fine. If you ever pass thousands of paths, batch them.
        paths = list(only_paths)
        if not paths:
            return []
        placeholders = ",".join("?" for _ in paths)
        rows = db.execute(
            f"""
            SELECT path, cacheKey, lastUpdated FROM tag_catalog
            WHERE dir = ? AND branch = ? AND artifactId = ?
              AND path IN ({placeholders})
            """,
            (tag.directory, tag.branch, tag.artifact_id, *paths),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT path, cacheKey, lastUpdated FROM tag_catalog
            WHERE dir = ? AND branch = ? AND artifactId = ?
            """,
            (tag.directory, tag.branch, tag.artifact_id),
        ).fetchall()
    return [(r["path"], r["cacheKey"], r["lastUpdated"]) for r in rows]



async def get_add_remove_for_tag(
    tag: IndexTag,
    current_files: FileStatsMap,
    read_file: Callable[[str], Awaitable[str]],
    *,
    only_paths: Optional[set[str]] = None,
) -> tuple[
    list[PathAndCacheKey],
    list[PathAndCacheKey],
    list[PathAndCacheKey],
    MarkCompleteCallback,
]:
    new_last_updated = int(time.time() * 1000)
    files = {
        path: stats
        for path, stats in current_files.items()
        if stats.size <= MAX_FILE_SIZE_BYTES
    }

    saved = get_saved_items_for_tag(tag, only_paths=only_paths)

    update_new_version: list[PathAndCacheKey] = []
    update_old_version: list[PathAndCacheKey] = []
    remove: list[PathAndCacheKey] = []
    update_last_updated: list[PathAndCacheKey] = []

    # Group saved rows by path
    path_groups: dict[str, dict] = {}
    for path, cache_key, last_updated in saved:
        if path not in path_groups:
            path_groups[path] = {
                "latest": {"last_updated": last_updated, "cache_key": cache_key},
                "all_versions": [{"cache_key": cache_key}],
            }
        else:
            group = path_groups[path]
            group["all_versions"].append({"cache_key": cache_key})
            if last_updated > group["latest"]["last_updated"]:
                group["latest"] = {
                    "last_updated": last_updated,
                    "cache_key": cache_key,
                }

    for path, group in path_groups.items():
        if path not in files:
            for version in group["all_versions"]:
                remove.append(
                    PathAndCacheKey(path=path, cache_key=version["cache_key"])
                )
        else:
            if group["latest"]["last_updated"] < files[path].last_modified:
                new_hash = calculate_hash(await read_file(path))
                if group["latest"]["cache_key"] != new_hash:
                    update_new_version.append(
                        PathAndCacheKey(path=path, cache_key=new_hash)
                    )
                    for version in group["all_versions"]:
                        update_old_version.append(
                            PathAndCacheKey(
                                path=path, cache_key=version["cache_key"]
                            )
                        )
                else:
                    update_last_updated.append(
                        PathAndCacheKey(
                            path=path,
                            cache_key=group["latest"]["cache_key"],
                        )
                    )
                    for version in group["all_versions"]:
                        if version["cache_key"] != group["latest"]["cache_key"]:
                            update_old_version.append(
                                PathAndCacheKey(
                                    path=path,
                                    cache_key=version["cache_key"],
                                )
                            )
            del files[path]

    # New files only — hash with limited concurrency
    sem = asyncio.Semaphore(_READ_CONCURRENCY)

    async def _hash_path(path: str) -> PathAndCacheKey:
        async with sem:
            contents = await read_file(path)
            return PathAndCacheKey(path=path, cache_key=calculate_hash(contents))

    add: list[PathAndCacheKey] = list(
        await asyncio.gather(*[_hash_path(p) for p in files.keys()])
    )

    db = SqliteDB.get()

    async def mark_complete(
        items: list[PathAndCacheKey],
        result_type: IndexResultType,
    ) -> None:
        action = _map_result_type(result_type)
        for item in items:
            path, cache_key = item.path, item.cache_key
            if action in (
                AddRemoveResultType.COMPUTE,
                AddRemoveResultType.ADD,
            ):
                db.execute(
                    """
                    REPLACE INTO tag_catalog
                        (path, cacheKey, lastUpdated, dir, branch, artifactId)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        path,
                        cache_key,
                        new_last_updated,
                        tag.directory,
                        tag.branch,
                        tag.artifact_id,
                    ),
                )
            elif action == AddRemoveResultType.REMOVE:
                db.execute(
                    """
                    DELETE FROM tag_catalog WHERE
                        cacheKey = ? AND path = ? AND dir = ?
                        AND branch = ? AND artifactId = ?
                    """,
                    (
                        cache_key,
                        path,
                        tag.directory,
                        tag.branch,
                        tag.artifact_id,
                    ),
                )
            elif action in (
                AddRemoveResultType.UPDATE_LAST_UPDATED,
                AddRemoveResultType.UPDATE_NEW_VERSION,
            ):
                db.execute(
                    """
                    UPDATE tag_catalog SET
                        cacheKey = ?, lastUpdated = ?
                    WHERE path = ? AND dir = ? AND branch = ? AND artifactId = ?
                    """,
                    (
                        cache_key,
                        new_last_updated,
                        path,
                        tag.directory,
                        tag.branch,
                        tag.artifact_id,
                    ),
                )
            # UPDATE_OLD_VERSION: no-op 
        db.commit()

    return (
        [*add, *update_new_version],
        [*remove, *update_old_version],
        update_last_updated,
        mark_complete,
    )


def get_tags_from_global_cache(
    cache_key: str, artifact_id: str
) -> list[IndexTag]:
    db = SqliteDB.get()
    rows = db.execute(
        """
        SELECT dir, branch, artifactId FROM global_cache
        WHERE cacheKey = ? AND artifactId = ?
        """,
        (cache_key, artifact_id),
    ).fetchall()
    return [
        IndexTag(
            directory=r["dir"],
            branch=r["branch"],
            artifact_id=r["artifactId"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Public entry: four lists + mark_complete that also updates global_cache
# ---------------------------------------------------------------------------

async def get_compute_delete_add_remove(
    tag: IndexTag,
    current_files: FileStatsMap,
    read_file: Callable[[str], Awaitable[str]],
    repo_name: Optional[str],
    *,
    only_paths: Optional[set[str]] = None,
) -> tuple[RefreshIndexResults, list[PathAndCacheKey], MarkCompleteCallback, MarkCompleteCallback]:
    
    add, remove, last_updated, mark_complete = await get_add_remove_for_tag(
        tag, current_files, read_file, only_paths=only_paths
    )

    compute: list[PathAndCacheKey] = []
    delete: list[PathAndCacheKey] = []
    add_tag: list[PathAndCacheKey] = []
    remove_tag: list[PathAndCacheKey] = []

    for item in add:
        existing = get_tags_from_global_cache(item.cache_key, tag.artifact_id)
        if existing:
            add_tag.append(item)
        else:
            compute.append(item)

    for item in remove:
        existing = get_tags_from_global_cache(item.cache_key, tag.artifact_id)
        if len(existing) > 1:
            remove_tag.append(item)
        else:
            delete.append(item)

    results = RefreshIndexResults(
        compute=compute,
        delete=delete,
        add_tag=add_tag,
        remove_tag=remove_tag,
    )

    global_cache = GlobalCacheCodeBaseIndex.create()

    async def mark_complete_with_global(
        items: list[PathAndCacheKey],
        result_type: IndexResultType,
    ) -> None:
        await mark_complete(items, result_type)

        partial = RefreshIndexResults()
        if result_type == IndexResultType.COMPUTE:
            partial.compute = items
        elif result_type == IndexResultType.DELETE:
            partial.delete = items
        elif result_type == IndexResultType.ADD_TAG:
            partial.add_tag = items
        elif result_type == IndexResultType.REMOVE_TAG:
            partial.remove_tag = items

        async for _ in global_cache.update(
            tag, partial, _noop_mark_complete, repo_name
        ):
            pass

    return (
        results,
        last_updated,
        mark_complete_with_global,
        mark_complete,
    )


async def _noop_mark_complete(
    items: list[PathAndCacheKey], result_type: IndexResultType
) -> None:
    return None


# ---------------------------------------------------------------------------
# Global cache index
# ---------------------------------------------------------------------------

class GlobalCacheCodeBaseIndex:
    relative_expected_time: float = 1.0
    artifact_id: str = "globalCache"

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    @classmethod
    def create(cls) -> GlobalCacheCodeBaseIndex:
        return cls(SqliteDB.get())

    async def update(
        self,
        tag: IndexTag,
        results: RefreshIndexResults,
        mark_complete: MarkCompleteCallback,
        repo_name: Optional[str],
    ):
        add = [*results.compute, *results.add_tag]
        remove = [*results.delete, *results.remove_tag]
        for item in remove:
            self._delete_or_remove_tag(item.cache_key, tag)
        for item in add:
            self._compute_or_add_tag(item.cache_key, tag)
        self.db.commit()
        yield IndexingProgressUpdate(
            progress=1.0, desc="Done updating global cache", status="done"
        )

    def _compute_or_add_tag(self, cache_key: str, tag: IndexTag) -> None:
        self.db.execute(
            """
            REPLACE INTO global_cache (cacheKey, dir, branch, artifactId)
            VALUES (?, ?, ?, ?)
            """,
            (cache_key, tag.directory, tag.branch, tag.artifact_id),
        )

    def _delete_or_remove_tag(self, cache_key: str, tag: IndexTag) -> None:
        self.db.execute(
            """
            DELETE FROM global_cache
            WHERE cacheKey = ? AND dir = ? AND branch = ? AND artifactId = ?
            """,
            (cache_key, tag.directory, tag.branch, tag.artifact_id),
        )


# ---------------------------------------------------------------------------
# Cross-process lock
# ---------------------------------------------------------------------------

class IndexLock:
    @staticmethod
    def is_locked() -> Optional[dict]:
        db = SqliteDB.get()
        row = db.execute(
            "SELECT locked, dirs, timestamp FROM indexing_lock WHERE locked = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "locked": bool(row["locked"]),
            "dirs": row["dirs"],
            "timestamp": row["timestamp"],
        }

    @staticmethod
    def lock(dirs: str) -> None:
        db = SqliteDB.get()
        db.execute(
            "INSERT INTO indexing_lock (locked, timestamp, dirs) VALUES (1, ?, ?)",
            (int(time.time() * 1000), dirs),
        )
        db.commit()

    @staticmethod
    def update_timestamp() -> None:
        db = SqliteDB.get()
        db.execute(
            "UPDATE indexing_lock SET timestamp = ? WHERE locked = 1",
            (int(time.time() * 1000),),
        )
        db.commit()

    @staticmethod
    def unlock() -> None:
        db = SqliteDB.get()
        db.execute("DELETE FROM indexing_lock WHERE locked = 1")
        db.commit()
        
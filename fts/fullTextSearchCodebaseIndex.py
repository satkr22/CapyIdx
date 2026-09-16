"""
Python port of FullTextSearchCodebaseIndex.ts.

Preserves behaviour:
    - FTS5 virtual table with trigram tokenizer
    - fts_metadata table linking to chunks
    - update(): compute / add_tag / remove_tag / delete
    - retrieve(): tag + path filtered BM25 search
    - path_weight_multiplier = 10.0
    - relative_expected_time = 0.2
    - artifact_id = "sqliteFts"

Assumes the underlying sqlite3.Connection has
row_factory = sqlite3.Row (which SqliteDB.get() sets).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from base.index_d import (
    Chunk,
    IndexTag,
    IndexingProgressUpdate,
    BranchAndDir,
    RetrieveConfig
)
from base.index_types import (
    CodebaseIndexer,
    IndexContext,
    IndexResultType,
    MarkCompleteCallback,
    RefreshIndexResults,
    
)
from chunker.chunkCodebaseIndex import ChunkCodebaseIndex
from utils.chunk_utils import tag_to_string
from utils.uri import get_uri_path_basename
from utils.parameters import RETRIEVAL_PARAMS


# ---------------------------------------------------------------------------
# FullTextSearchCodebaseIndex
# ---------------------------------------------------------------------------

class FullTextSearchCodebaseIndex(CodebaseIndexer):
    artifact_id = "sqliteFts"
    relative_expected_time = 0.2
    path_weight_multiplier = 10.0

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row # safe to set again
        # self._create_tables()

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #

    def _create_tables(self) -> None:
        self.db.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                path,
                content,
                tokenize = 'trigram'
            );

            CREATE TABLE IF NOT EXISTS fts_metadata (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                chunkId INTEGER NOT NULL,
                FOREIGN KEY (chunkId) REFERENCES chunks (id),
                FOREIGN KEY (id) REFERENCES fts (rowid)
            );
            """
        )
        self.db.commit()

    # ------------------------------------------------------------------ #
    # Update
    # ------------------------------------------------------------------ #

    async def update(
        self,
        tag: IndexTag,
        context: IndexContext,
        results: RefreshIndexResults,
        mark_complete: MarkCompleteCallback,
    ) -> AsyncIterator[IndexingProgressUpdate]:
        
        self._create_tables()  # more defenside '_create_tables' here as it re-asserts the correct schema of table for every run
        total_compute = len(results.compute)

        # Compute ---------------------------------------------------- #
        for i, item in enumerate(results.compute):
            
            
            # --- drop any prior FTS rows for this (path, cacheKey) no double indexing because of any crash ---
            self.db.execute(
                """
                DELETE FROM fts WHERE rowid IN (
                    SELECT id FROM fts_metadata WHERE path = ? AND cacheKey = ?
                )
                """,
                (item.path, item.cache_key),
            )
            self.db.execute(
                "DELETE FROM fts_metadata WHERE path = ? AND cacheKey = ?",
                (item.path, item.cache_key),
            )
            # --------------------------------------------------------------
            
            chunks = self.db.execute(
                "SELECT * FROM chunks WHERE path = ? AND cacheKey = ?",
                (item.path, item.cache_key),
            ).fetchall()

            for chunk in chunks:
                cursor = self.db.execute(
                    "INSERT INTO fts (path, content) VALUES (?, ?)",
                    (item.path, chunk["content"]),
                )
                last_id = cursor.lastrowid

                self.db.execute(
                    """
                    INSERT INTO fts_metadata (id, path, cacheKey, chunkId)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        path = excluded.path,
                        cacheKey = excluded.cacheKey,
                        chunkId = excluded.chunkId
                    """,
                    (last_id, item.path, item.cache_key, chunk["id"]),
                )

            self.db.commit()

            yield IndexingProgressUpdate(
                progress=i / total_compute if total_compute > 0 else 0.0,
                desc=f"Indexing {get_uri_path_basename(item.path)}",
                status="indexing",
            )

            await mark_complete([item], IndexResultType.COMPUTE)

        # Add tag ---------------------------------------------------- #
        for item in results.add_tag:
            await mark_complete([item], IndexResultType.ADD_TAG)

        # Remove tag ------------------------------------------------- #
        for item in results.remove_tag:
            await mark_complete([item], IndexResultType.REMOVE_TAG)

        # Delete ----------------------------------------------------- #
        for item in results.delete:
            self.db.execute(
                """
                DELETE FROM fts WHERE rowid IN (
                    SELECT id FROM fts_metadata WHERE path = ? AND cacheKey = ?
                )
                """,
                (item.path, item.cache_key),
            )
            self.db.execute(
                "DELETE FROM fts_metadata WHERE path = ? AND cacheKey = ?",
                (item.path, item.cache_key),
            )
            self.db.commit()

            await mark_complete([item], IndexResultType.DELETE)

    # ------------------------------------------------------------------ #
    # Retrieve
    # ------------------------------------------------------------------ #

    async def retrieve(self, config: RetrieveConfig) -> list[Chunk]:
        query = self._build_retrieve_query(config)
        parameters = self._get_retrieve_query_parameters(config)

        results = self.db.execute(query, parameters).fetchall()

        threshold = (
            config.bm25_threshold
            if config.bm25_threshold is not None
            else RETRIEVAL_PARAMS["bm25Threshold"]
        )
        results = [r for r in results if r["rank"] <= threshold]

        if not results:
            return []

        placeholders = ",".join("?" for _ in results)
        chunk_ids = [r["chunkId"] for r in results]

        chunks = self.db.execute(
            f"SELECT * FROM chunks WHERE id IN ({placeholders})",
            chunk_ids,
        ).fetchall()
        
        chunk_map = {c["id"]: c for c in chunks}
        
        seen: set[int] = set()
        out: list[Chunk] = []
        
        for result in results:
            cid = result["chunkId"]
            if cid in seen:
                continue
            seen.add(cid)

            row = chunk_map.get(cid)
            if row is None:
                # Orphaned fts_metadata.chunkId — chunks row already deleted,
                # or FK enforcement is off. Skip rather than crash.
                continue
            
            out.append(
                Chunk(
                    filepath=row["path"],
                    index=row["idx"],
                    start_line=row["startLine"],
                    end_line=row["endLine"],
                    content=row["content"],
                    digest=row["cacheKey"],
                )
            )
            
        return out

    # ------------------------------------------------------------------ #
    # Query construction
    # ------------------------------------------------------------------ #

    def _build_tag_filter(self, tags: list[BranchAndDir]) -> str:
        tag_strings = self._convert_tags(tags)
        placeholders = ",".join("?" for _ in tag_strings)
        return f"AND chunk_tags.tag IN ({placeholders})"

    def _build_path_filter(
        self, filter_paths: Optional[list[str]]
    ) -> str:
        if not filter_paths:
            return ""
        placeholders = ",".join("?" for _ in filter_paths)
        return f"AND fts_metadata.path IN ({placeholders})"

    def _build_retrieve_query(self, config: RetrieveConfig) -> str:
        return f"""
            SELECT fts_metadata.chunkId, fts_metadata.path, fts.content, rank
            FROM fts
            JOIN fts_metadata ON fts.rowid = fts_metadata.id
            JOIN chunk_tags ON fts_metadata.chunkId = chunk_tags.chunkId
            WHERE fts MATCH ?
            {self._build_tag_filter(config.tags)}
            {self._build_path_filter(config.filter_paths)}
            ORDER BY bm25(fts, {self.path_weight_multiplier})
            LIMIT ?
        """

    def _get_retrieve_query_parameters(self, config: RetrieveConfig) -> list:
        tag_strings = self._convert_tags(config.tags)
        return [
            config.text.replace("?", ""),
            *tag_strings,
            *(config.filter_paths or []),
            math.ceil(config.n),
        ]

    def _convert_tags(self, tags: list[BranchAndDir]) -> list[str]:
        # Notice: the "chunks" artifact_id is used because of linking
        # between tables.
        return [
            tag_to_string(
                IndexTag(
                    directory=t.directory,
                    branch=t.branch,
                    artifact_id=ChunkCodebaseIndex.artifact_id,
                )
            )
            for t in tags
        ]
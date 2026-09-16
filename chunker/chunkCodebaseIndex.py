from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import AsyncIterator

from base.index_d import (
    Chunk,
    FileSystem,
    IndexTag,
    IndexingProgressUpdate,
)
from base.index_types import (
    CodebaseIndexer,
    IndexContext,
    IndexResultType,
    MarkCompleteCallback,
    PathAndCacheKey,
    RefreshIndexResults,
)
from chunker.chunk import chunk_document, ChunkDocumentParam, should_chunk
from utils.chunk_utils import tag_to_string
from utils.uri import get_uri_path_basename


class ChunkCodebaseIndex(CodebaseIndexer):
    """
    Generic Python port of Continue's ChunkCodebaseIndex.

    Infrastructure removed:
        - ContinueServerClient
        - Remote cache
        - SqliteDb singleton

    Caller provides:
        - sqlite3.Connection
        - FileSystem
        - RefreshIndexResults
        - mark_complete callback
    """

    artifact_id = "chunks"
    relative_expected_time = 1.0

    def __init__(
        self,
        db: sqlite3.Connection,
        filesystem: FileSystem,
        max_chunk_size: int = 1024,
    ):
        self.db = db
        self.db.row_factory = sqlite3.Row 
        self.fs = filesystem
        self.max_chunk_size = max_chunk_size
        self.create_tables()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def update(
        self,
        tag: IndexTag,
        context: IndexContext,
        results: RefreshIndexResults,
        mark_complete: MarkCompleteCallback,
    ) -> AsyncIterator[IndexingProgressUpdate]:

        tag_string = tag_to_string(tag)
        progress = 0.0

        # -------------------------------------------------------------- #
        # Compute new chunks
        # -------------------------------------------------------------- #

        if results.compute:
            folder = os.path.basename(os.path.dirname(results.compute[0].path))

            yield IndexingProgressUpdate(
                progress=progress,
                desc=f"Chunking files in {folder}",
                status="indexing",
            )

            chunks = await self.compute_chunks(results.compute)
            self.insert_chunks(tag_string, chunks)

            await mark_complete(results.compute, IndexResultType.COMPUTE)

        # -------------------------------------------------------------- #
        # Add tag
        # -------------------------------------------------------------- #

        total = max(len(results.add_tag), 1)

        for item in results.add_tag:
            self.db.execute(
                """
                INSERT OR IGNORE INTO chunk_tags (chunkId, tag)
                SELECT id, ?
                FROM chunks
                WHERE cacheKey = ?
                """,
                (tag_string, item.cache_key),
            )

            self.db.commit()

            await mark_complete([item], IndexResultType.ADD_TAG)

            progress += 1 / total / 4

            yield IndexingProgressUpdate(
                progress=progress,
                desc=f"Adding {get_uri_path_basename(item.path)}",
                status="indexing",
            )

        # -------------------------------------------------------------- #
        # Remove tag
        # -------------------------------------------------------------- #
        
        total = max(len(results.remove_tag), 1)

        for item in results.remove_tag:
            self.db.execute(
                """
                DELETE FROM chunk_tags
                WHERE tag = ?
                  AND chunkId IN (
                        SELECT id FROM chunks
                        WHERE cacheKey = ? AND path = ?
                  )
                """,
                (tag_string, item.cache_key, item.path),
            )

            self.db.commit()

            await mark_complete([item], IndexResultType.REMOVE_TAG)

            progress += 1 / total / 4

            yield IndexingProgressUpdate(
                progress=progress,
                desc=f"Removing {get_uri_path_basename(item.path)}",
                status="indexing",
            )

        # -------------------------------------------------------------- #
        # Delete chunks
        # -------------------------------------------------------------- #

        total = max(len(results.delete), 1)

        for item in results.delete:
            row = self.db.execute(
                "SELECT id FROM chunks WHERE cacheKey = ?",
                (item.cache_key,),
            ).fetchone()

            if row:
                chunk_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]

                self.db.execute(
                    "DELETE FROM chunks WHERE id = ?",
                    (chunk_id,),
                )

                self.db.execute(
                    "DELETE FROM chunk_tags WHERE chunkId = ?",
                    (chunk_id,),
                )

                self.db.commit()

            await mark_complete([item], IndexResultType.DELETE)

            progress += 1 / total / 4

            yield IndexingProgressUpdate(
                progress=progress,
                desc=f"Removing {get_uri_path_basename(item.path)}",
                status="indexing",
            )

    # ------------------------------------------------------------------ #
    # Chunk computation
    # ------------------------------------------------------------------ #

    async def pack_to_chunks(self, pack: PathAndCacheKey) -> list[Chunk]:
        contents = await self.fs.read_file(pack.path)

        if not should_chunk(pack.path, contents):
            return []

        chunks: list[Chunk] = []

        async for chunk in chunk_document(
            ChunkDocumentParam(
                filepath=pack.path,
                contents=contents,
                maxChunkSize=self.max_chunk_size,
                digest=pack.cache_key,
            )
        ):
            chunks.append(chunk)

        return chunks

    async def compute_chunks(
        self,
        packs: list[PathAndCacheKey],
    ) -> list[Chunk]:
        results = await asyncio.gather(
            *(self.pack_to_chunks(p) for p in packs)
        )

        return [chunk for group in results for chunk in group]

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #

    def create_tables(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cacheKey TEXT NOT NULL,
                path TEXT NOT NULL,
                idx INTEGER NOT NULL,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL,
                content TEXT NOT NULL
            )
            """
        )

        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS chunk_tags(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                chunkId INTEGER NOT NULL,
                FOREIGN KEY(chunkId) REFERENCES chunks(id),
                UNIQUE(tag, chunkId)
            )
            """
        )

        self.db.commit()

    def insert_chunks(
        self,
        tag_string: str,
        chunks: list[Chunk],
    ) -> None:
        """
        Exact equivalent of Continue's BEGIN / COMMIT transaction.
        """
        with self.db:
            for chunk in chunks:
                cursor = self.db.execute(
                    """
                    INSERT INTO chunks(
                        cacheKey,
                        path,
                        idx,
                        startLine,
                        endLine,
                        content
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.digest,
                        chunk.filepath,
                        chunk.index,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.content,
                    ),
                )

                self.db.execute(
                    """
                    INSERT INTO chunk_tags(chunkId, tag)
                    VALUES (?, ?)
                    """,
                    (cursor.lastrowid, tag_string),
                )
from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import AsyncIterator
from uuid import uuid4
from time import perf_counter

from base.index_d import (
    Chunk,
    ChunkingResult,
    FileSystem,
    IndexTag,
    IndexingProgressUpdate,
    Symbol,
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
        
        self.read_time = 0
        self.chunk_time = 0
        self.db_time = 0
        self.parse_time = []

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
        # Compute new chunks — one file at a time
        # -------------------------------------------------------------- #
        if results.compute:
            folder = os.path.basename(os.path.dirname(results.compute[0].path))

            yield IndexingProgressUpdate(
                progress=progress,
                desc=f"Chunking files in {folder}",
                status="indexing",
            )
            
            for item in results.compute:
                
                
                t3 = perf_counter()
                chunks, result = await self.pack_to_chunks(item, self.parse_time)
                self.chunk_time += perf_counter() - t3
                # Wipe any prior rows for this file (cascade drops chunk_tags)
                t2 = perf_counter()
                self.delete_file_chunks_and_symbols(item.cache_key)
                self.db_time += perf_counter()-t2

                # FK order: symbols first, then chunks.
                
                t2 = perf_counter()
                self.insert_symbols(item.cache_key, item.path, result.symbols)
                self.insert_chunks(tag_string, chunks)
                self.db_time += perf_counter()-t2
                
                await mark_complete([item], IndexResultType.COMPUTE)

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
        # Delete
        # -------------------------------------------------------------- #
        total = max(len(results.delete), 1)

        for item in results.delete:
            self.delete_file_chunks_and_symbols(item.cache_key)

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

    async def pack_to_chunks(
        self,
        pack: PathAndCacheKey,
        parse_time: list
    ) -> tuple[list[Chunk], ChunkingResult]:

        t0 = perf_counter()
        contents = await self.fs.read_file(pack.path)
        self.read_time += perf_counter() - t0
        
        if not should_chunk(pack.path, contents):
            return [], ChunkingResult()

        chunks: list[Chunk] = []
        result = ChunkingResult()

        t1 = perf_counter()
        async for item in chunk_document(
            ChunkDocumentParam(
                filepath=pack.path,
                contents=contents,
                maxChunkSize=self.max_chunk_size,
                digest=pack.cache_key,
            ),
            parse_time
        ):
            self.chunk_time += perf_counter() - t1
            if isinstance(item, ChunkingResult):
                result = item
            else:
                chunks.append(item)
            
        return chunks, result

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #

    def create_tables(self) -> None:
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS symbols(
                id         TEXT PRIMARY KEY,
                type       TEXT NOT NULL,
                name       TEXT NOT NULL,
                parentId   TEXT,
                startLine  INTEGER NOT NULL,
                endLine    INTEGER NOT NULL,
                cacheKey   TEXT NOT NULL,
                path       TEXT NOT NULL,
                FOREIGN KEY(parentId) REFERENCES symbols(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_symbols_parent   ON symbols(parentId);
            CREATE INDEX IF NOT EXISTS idx_symbols_cachekey ON symbols(cacheKey);
            CREATE INDEX IF NOT EXISTS idx_symbols_path     ON symbols(path);

            CREATE TABLE IF NOT EXISTS chunks(
                id          TEXT PRIMARY KEY,
                symbolId    TEXT NOT NULL,
                cacheKey    TEXT NOT NULL,
                path        TEXT NOT NULL,
                idx         INTEGER NOT NULL,
                pieceIndex  INTEGER NOT NULL,
                pieceCount  INTEGER NOT NULL,
                prevChunk   TEXT,
                nextChunk   TEXT,
                signature   TEXT,
                startLine   INTEGER NOT NULL,
                endLine     INTEGER NOT NULL,
                content     TEXT NOT NULL,
                FOREIGN KEY(symbolId) REFERENCES symbols(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_symbol   ON chunks(symbolId);
            CREATE INDEX IF NOT EXISTS idx_chunks_cachekey ON chunks(cacheKey);
            CREATE INDEX IF NOT EXISTS idx_chunks_path     ON chunks(path);

            CREATE TABLE IF NOT EXISTS chunk_tags(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                tag      TEXT NOT NULL,
                chunkId  TEXT NOT NULL,
                FOREIGN KEY(chunkId) REFERENCES chunks(id) ON DELETE CASCADE,
                UNIQUE(tag, chunkId)
            );
            CREATE INDEX IF NOT EXISTS idx_chunk_tags_tag     ON chunk_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_chunk_tags_chunkid ON chunk_tags(chunkId);
            """
        )
        self.db.commit()

    def delete_file_chunks_and_symbols(self, cache_key: str) -> None:
        with self.db:
            # Deleting chunks first drops their chunk_tags via FK cascade,
            # then symbols (their children cascade too).
            self.db.execute("DELETE FROM chunks  WHERE cacheKey = ?", (cache_key,))
            self.db.execute("DELETE FROM symbols WHERE cacheKey = ?", (cache_key,))

    def insert_symbols(
        self,
        cache_key: str,
        filepath: str,
        symbols: list[Symbol],
    ) -> None:
        """
        Symbols come out of the chunker in topological order
        (file → class → method → nested), so FK parentId is always
        satisfied when the row is inserted.
        """
        seen: set[str] = set()
        with self.db:
            for sym in symbols:
                if sym.parent_id is not None and str(sym.parent_id) not in seen:
                    raise RuntimeError(
                        f"Parent {sym.parent_id} inserted after child {sym.id}"
                    )
                self.db.execute(
                    """
                    INSERT OR REPLACE INTO symbols(
                        id, type, name, parentId, startLine, endLine,
                        cacheKey, path
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(sym.id),
                        sym.type,
                        sym.name,
                        str(sym.parent_id) if sym.parent_id else None,
                        sym.start_line,
                        sym.end_line,
                        cache_key,
                        filepath,
                    ),
                )
                seen.add(str(sym.id))

    def insert_chunks(
        self,
        tag_string: str,
        chunks: list[Chunk],
    ) -> None:
        with self.db:
            for ch in chunks:
                self.db.execute(
                    """
                    INSERT OR REPLACE INTO chunks(
                        id, symbolId, cacheKey, path, idx,
                        pieceIndex, pieceCount, prevChunk, nextChunk,
                        signature, startLine, endLine, content
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(ch.id),
                        str(ch.symbol_id),
                        ch.digest,
                        ch.filepath,
                        ch.index,
                        ch.piece_index,
                        ch.piece_count,
                        str(ch.prev_chunk) if ch.prev_chunk else None,
                        str(ch.next_chunk) if ch.next_chunk else None,
                        ch.signature,
                        ch.start_line,
                        ch.end_line,
                        ch.content,
                    ),
                )
                self.db.execute(
                    """
                    INSERT OR IGNORE INTO chunk_tags(chunkId, tag)
                    VALUES (?, ?)
                    """,
                    (str(ch.id), tag_string),
                )

    def _insert_or_raise(self, sql: str, params: tuple) -> None:
        try:
            self.db.execute(sql, params)
        except sqlite3.IntegrityError as e:
            raise sqlite3.IntegrityError(
                f"{e}\nSQL: {sql}\nPARAMS: {params}"
            ) from e
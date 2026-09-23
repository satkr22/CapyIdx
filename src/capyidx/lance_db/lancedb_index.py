from __future__ import annotations

import importlib
import asyncio
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Protocol

from capyidx.base.index_d import (
    BranchAndDir,
    Chunk,
    IndexTag,
    IndexingProgressUpdate,
)
from capyidx.base.index_types import (
    CodebaseIndexer,
    IndexContext,
    IndexResultType,
    MarkCompleteCallback,
    PathAndCacheKey,
    RefreshIndexResults,
    FileSystem
)
from capyidx.chunker.basic import basic_chunker
from capyidx.chunker.chunk import ChunkDocumentParam, chunk_document, should_chunk
from capyidx.utils.chunk_utils import tag_to_string
from capyidx.utils.paths import get_lance_db_path, migrate
from capyidx.utils.uri1 import get_uri_path_basename
from capyidx.embeddings.base import Embeddings


@dataclass
class ItemWithChunks:
    item: PathAndCacheKey
    chunks: List[Chunk]


class LanceDbIndex(CodebaseIndexer):
    lance: Any = None

    relative_expected_time: float = 13

    @staticmethod
    async def create(
        db: sqlite3.Connection,
        embeddings_provider: Embeddings,
        filesystem: FileSystem,
    ) -> Optional[LanceDbIndex]:
        try:
            LanceDbIndex.lance = importlib.import_module("lancedb")
            return LanceDbIndex(db, embeddings_provider, filesystem)
        except Exception as e:
            print("Failed to import lancedb. If you're on a pre-Haswell x86_64 CPU,\ninstall the compatibility wheel: pip uninstall lancedb && pip install lancedb-compat':", e)
            return None

    def __init__(
        self,
        db: sqlite3.Connection,
        embeddings_provider: Embeddings,
        filesystem: FileSystem,
    ):
        if LanceDbIndex.lance is None:
            raise RuntimeError("LanceDB not initialized")
        self.db = db
        self.embeddings_provider = embeddings_provider
        self.artifact_id = f"vectordb::{embeddings_provider.embedding_id}"
        self.fs = filesystem

    def table_name_for_tag(self, tag: IndexTag) -> str:
        return re.sub(r"[^\w\-_.]", "", tag_to_string(tag))

    async def create_sqlite_cache_table(self, db: sqlite3.Connection):
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS lance_db_cache (
                uuid TEXT PRIMARY KEY,
                cacheKey TEXT NOT NULL,
                path TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                vector TEXT NOT NULL,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL,
                contents TEXT NOT NULL
            )
            """
        )
        db.commit()

        async def _migrate():
            pragma = db.execute("PRAGMA table_info(lance_db_cache)").fetchall()
            has_artifact_id_col = any(row["name"] == "artifact_id" for row in pragma)
            if not has_artifact_id_col:
                db.execute(
                    "ALTER TABLE lance_db_cache ADD COLUMN artifact_id TEXT NOT NULL DEFAULT 'UNDEFINED'"
                )
                db.commit()

        await migrate("lancedb_sqlite_artifact_id_column", _migrate)
        
    async def get_existing_chunks(self, item: PathAndCacheKey) -> list[Chunk]:
        rows = self.db.execute(
            """
            SELECT id, cacheKey, path, idx, startLine, endLine, content
            FROM chunks
            WHERE path = ? AND cacheKey = ?
            ORDER BY idx
            """,
            (item.path, item.cache_key),
        ).fetchall()

        return [
            Chunk(
                id=row["id"],
                content=row["content"],
                start_line=row["startLine"],
                end_line=row["endLine"],
                digest=row["cacheKey"],
                filepath=row["path"],
                index=row["idx"], 
            )
            for row in rows
        ]

    async def compute_rows(self, items: List[PathAndCacheKey]) -> List[dict]:
        chunk_map = await self.collect_chunks(items)
        all_chunks = [chunk for item in chunk_map.values() for chunk in item.chunks]
        embeddings = await self.get_embeddings(all_chunks)

        for i in range(len(embeddings) - 1, -1, -1):
            if embeddings[i] is None:
                chunk = all_chunks[i]
                item_with_chunks = chunk_map.get(chunk.filepath)
                if item_with_chunks:
                    chunks = item_with_chunks.chunks
                    try:
                        index = chunks.index(chunk)
                        chunks.pop(index)
                    except ValueError:
                        pass
                embeddings.pop(i)

        return self.create_lance_db_rows(chunk_map, embeddings)

    async def collect_chunks(
        self, items: List[PathAndCacheKey]
    ) -> Dict[str, ItemWithChunks]:
        chunk_map: Dict[str, ItemWithChunks] = {}
        for item in items:
            try:
                chunks = await self.get_existing_chunks(item)
                if not chunks:
                    continue
                chunk_map[item.path] = ItemWithChunks(item=item, chunks=chunks)
            except Exception as e:
                print(f"LanceDBIndex, skipping {item.path}: {e}")
        return chunk_map

    async def get_chunks(self, item: PathAndCacheKey, content: str) -> List[Chunk]:
        if not self.embeddings_provider:
            return []
        chunks: List[Chunk] = []
        chunk_params = ChunkDocumentParam(
            filepath=item.path,
            contents=content,
            maxChunkSize=self.embeddings_provider.max_embedding_chunk_size,
            digest=item.cache_key,
        )
        async for chunk in chunk_document(chunk_params):
            if isinstance(chunk, Chunk):
                if len(chunk.content) == 0:
                    raise ValueError("did not chunk properly")
                chunks.append(chunk)
        return chunks

    async def get_embeddings(self, chunks: List[Chunk]) -> List[List[float]]:
        if not self.embeddings_provider:
            return []
        try:
            return await self.embeddings_provider.embed([c.content for c in chunks])
        except Exception as e:
            raise RuntimeError(
                f"Failed to generate embeddings for {len(chunks)} chunks "
                f"with provider: {self.embeddings_provider.embedding_id}: {e}"
            ) from e

    def create_lance_db_rows(
        self,
        chunk_map: Dict[str, ItemWithChunks],
        embeddings: List[List[float]],
    ) -> List[dict]:
        results: List[dict] = []
        embedding_index = 0
        for path, item_with_chunks in chunk_map.items():
            item = item_with_chunks.item
            for chunk in item_with_chunks.chunks:
                results.append(
                    {
                        "path": path,
                        "cachekey": item.cache_key,
                        "uuid": chunk.id,
                        "vector": embeddings[embedding_index],
                        "startLine": chunk.start_line,
                        "endLine": chunk.end_line,
                        "contents": chunk.content,
                    }
                )
                embedding_index += 1
        return results

    def parse_vector(self, vector: str) -> List[float]:
        try:
            return json.loads(vector)
        except json.JSONDecodeError:
            try:
                return json.loads(f"[{vector}]")
            except json.JSONDecodeError as e:
                raise ValueError(f"Failed to parse vector: {vector}") from e


    async def update(
        self,
        tag: IndexTag,
        context: IndexContext,
        results: RefreshIndexResults,
        mark_complete: MarkCompleteCallback,
    ) -> AsyncGenerator[IndexingProgressUpdate, None]:
        lance = LanceDbIndex.lance
        if lance is None:
            raise RuntimeError("LanceDB not initialized")

        db = self.db
        await self.create_sqlite_cache_table(db)

        lance_table_name = self.table_name_for_tag(tag)

        # connect + tableNames both hit the filesystem and can block.
        lance_db = await asyncio.to_thread(lance.connect, str(get_lance_db_path()))
        existing_lance_tables = list(
            await asyncio.to_thread(lance_db.table_names)
        )

        lance_table = None
        need_to_create_lance_table = lance_table_name not in existing_lance_tables

        async def add_computed_lance_db_rows(
            path_and_cache_keys: List[PathAndCacheKey],
            computed_rows: List[dict],
        ):
            nonlocal lance_table, need_to_create_lance_table
            if lance_table is not None:
                if computed_rows:
                    await asyncio.to_thread(lance_table.add, computed_rows)
            elif lance_table_name in existing_lance_tables:
                lance_table = await asyncio.to_thread(
                    lance_db.open_table, lance_table_name
                )
                need_to_create_lance_table = False
                if computed_rows:
                    await asyncio.to_thread(lance_table.add, computed_rows)
            elif computed_rows:
                lance_table = await asyncio.to_thread(
                    lance_db.create_table, lance_table_name, computed_rows
                )
                need_to_create_lance_table = False

            await mark_complete(path_and_cache_keys, IndexResultType.COMPUTE)

        yield IndexingProgressUpdate(
            progress=0,
            desc=f"Computing embeddings for {len(results.compute)} "
            f"{self.format_list_plurality('file', len(results.compute))}",
            status="indexing",
        )

        db_rows = await self.compute_rows(results.compute)
        await self.insert_rows(db, db_rows)
        await add_computed_lance_db_rows(results.compute, db_rows)
        accumulated_progress = 0.0

        for item in results.add_tag:
            path = item.path
            cache_key = item.cache_key
            cached_items = db.execute(
                "SELECT * FROM lance_db_cache WHERE cacheKey = ? AND artifact_id = ?",
                (cache_key, self.artifact_id),
            ).fetchall()

            lance_rows: List[dict] = []
            for cached_item in cached_items:
                try:
                    vector = self.parse_vector(cached_item["vector"])
                    lance_rows.append(
                        {
                            "path": path,
                            "uuid": cached_item["uuid"],
                            "startLine": cached_item["startLine"],
                            "endLine": cached_item["endLine"],
                            "contents": cached_item["contents"],
                            "cachekey": cache_key,
                            "vector": vector,
                        }
                    )
                except Exception as e:
                    print(
                        f"LanceDBIndex, skipping {cached_item['path']} due to invalid vector JSON:\n"
                        f"{cached_item['vector']}\n\nError: {e}"
                    )

            if lance_rows:
                if need_to_create_lance_table:
                    lance_table = await asyncio.to_thread(
                        lance_db.create_table, lance_table_name, lance_rows
                    )
                    need_to_create_lance_table = False
                elif lance_table is None:
                    lance_table = await asyncio.to_thread(
                        lance_db.open_table, lance_table_name
                    )
                    need_to_create_lance_table = False
                    await asyncio.to_thread(lance_table.add, lance_rows)
                else:
                    await asyncio.to_thread(lance_table.add, lance_rows)

            await mark_complete([item], IndexResultType.ADD_TAG)
            if results.add_tag:
                accumulated_progress += 1 / len(results.add_tag) / 3
            yield IndexingProgressUpdate(
                progress=accumulated_progress,
                desc=f"Indexing {get_uri_path_basename(path)}",
                status="indexing",
            )

        if not need_to_create_lance_table:
            to_del = [*results.remove_tag, *results.delete]
            if lance_table is None:
                lance_table = await asyncio.to_thread(
                    lance_db.open_table, lance_table_name
                )

            for item in to_del:
                path = item.path
                cache_key = item.cache_key
                # predicate = f"cachekey = '{cache_key}' AND path = '{path}'"
                
                safe_cache = cache_key.replace("'", "''")
                safe_path = path.replace("'", "''")


                predicate = (
                    f"cachekey = '{safe_cache}' "
                    f"AND path = '{safe_path}'"
                )
                
                await asyncio.to_thread(lance_table.delete, predicate)
                if to_del:
                    accumulated_progress += 1 / len(to_del) / 3
                yield IndexingProgressUpdate(
                    progress=accumulated_progress,
                    desc=f"Stashing {get_uri_path_basename(path)}",
                    status="indexing",
                )

        await mark_complete(results.remove_tag, IndexResultType.REMOVE_TAG)

        for item in results.delete:
            path = item.path
            cache_key = item.cache_key
            db.execute(
                "DELETE FROM lance_db_cache WHERE cacheKey = ? AND path = ? AND artifact_id = ?",
                (cache_key, path, self.artifact_id),
            )
            db.commit()
            if results.delete:
                accumulated_progress += 1 / len(results.delete) / 3
            yield IndexingProgressUpdate(
                progress=accumulated_progress,
                desc=f"Removing {get_uri_path_basename(path)}",
                status="indexing",
            )

        await mark_complete(results.delete, IndexResultType.DELETE)

        yield IndexingProgressUpdate(
            progress=1,
            desc="Completed Calculating Embeddings",
            status="done",
        )

    async def _retrieve_for_tag(
        self,
        tag: IndexTag,
        n: int,
        directory: Optional[str],
        vector: List[float],
        db: Any,
    ) -> List[dict]:
        table_name = self.table_name_for_tag(tag)
        table_names = await asyncio.to_thread(db.table_names)
        if table_name not in table_names:
            print(f"Table not found in LanceDB {table_name}")
            return []

        table = await asyncio.to_thread(db.open_table, table_name)

        def _search_sync() -> List[dict]:
            # This entire chain is CPU + disk work; keep it in one sync function
            # so it runs as a single unit on the worker thread.
            if directory:
                return (
                    table.search(vector)
                    .where(f"path LIKE '{directory}%'")
                    .limit(300)
                    .to_list()
                )
            return table.search(vector).limit(n).to_list()

        results = await asyncio.to_thread(_search_sync)
        return results[:n]

    async def retrieve(
        self,
        query: str,
        n: int,
        tags: List[BranchAndDir],
        filter_directory: Optional[str],
    ) -> List[tuple[Chunk, float, str]]:
        lance = LanceDbIndex.lance
        if lance is None or not self.embeddings_provider:
            return []

        chunks: List[dict] = []
        async for chunk in basic_chunker(
            query, self.embeddings_provider.max_embedding_chunk_size
        ):
            chunks.append(chunk)

        vector: Optional[List[float]] = None
        try:
            embeddings = await self.embeddings_provider.embed(
                [c["content"] for c in chunks]
            )
            if embeddings:
                vector = embeddings[0]
        except Exception:
            embeddings = await self.embeddings_provider.embed([query])
            if embeddings:
                vector = embeddings[0]

        if vector is None:
            return []

        db = await asyncio.to_thread(lance.connect, str(get_lance_db_path()))

        all_results: List[dict] = []
        for tag in tags:
            results = await self._retrieve_for_tag(
                IndexTag(
                    directory=tag.directory,
                    branch=tag.branch,
                    artifact_id=self.artifact_id,
                ),
                n,
                filter_directory,
                vector,
                db,
            )
            all_results.extend(results)

        all_results.sort(key=lambda x: x["_distance"])
        all_results = all_results[:n]

        sqlite_db = self.db
        if not all_results:
            return []
        uuids = ",".join(f"'{r['uuid']}'" for r in all_results)
        placeholders = ",".join("?" for _ in all_results)

        data = sqlite_db.execute(
            f"SELECT * FROM lance_db_cache WHERE uuid IN ({placeholders})",
            [r["uuid"] for r in all_results],
        ).fetchall()
        
        row_map = {r["uuid"]: r for r in data}
        seen: set[str] = set()
        out: list[tuple[Chunk, float, str]] = []
        
        for result in all_results:
            uid = result["uuid"]
            
            if uid in seen:
                continue
            seen.add(uid)
            
            row = row_map.get(uid)
            if row is None:
                # Orphaned fts_metadata.chunkId — chunks row already deleted,
                # or FK enforcement is off. Skip rather than crash.
                continue
            
            dist = float(result["_distance"])
            # Convert L2 : cosine similarity for unit-norm vectors:
            cos_sim = 1.0 - (dist * dist) / 2.0
            # Clamp to [0, 1] to be safe
            cos_sim = max(0.0, min(1.0, cos_sim))
            
            out.append(
                (
                    Chunk(
                        digest=row["cacheKey"],
                        filepath=row["path"],
                        start_line=row["startLine"],
                        end_line=row["endLine"],
                        index=0,
                        content=row["contents"],
                    ),
                    cos_sim, # vector score
                    result["uuid"] # chunk_id
                )   
            )
        return out

    async def insert_rows(self, db: sqlite3.Connection, rows: List[dict]):
        try:
            if not rows:
                return
            
            delete_params = list({
                (r["cachekey"], r["path"], self.artifact_id)
                for r in rows
            })
            
            db.executemany(
                """
                DELETE FROM lance_db_cache
                WHERE cacheKey = ? AND path = ? AND artifact_id = ?
                """,
                delete_params,
            )
            
            db.executemany(
                "INSERT INTO lance_db_cache (uuid, cacheKey, path, artifact_id, vector, startLine, endLine, contents) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["uuid"],
                        r["cachekey"],
                        r["path"],
                        self.artifact_id,
                        json.dumps(r["vector"]),
                        r["startLine"],
                        r["endLine"],
                        r["contents"],
                    )
                    for r in rows
                ],
            )
            db.commit()
        except Exception as e:
            db.rollback()
            raise RuntimeError("error inserting into lance_db_cache table") from e

    def format_list_plurality(self, word: str, length: int) -> str:
        return word if length <= 1 else f"{word}s"
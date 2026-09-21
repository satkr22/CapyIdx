"""Diagnostic harness for the isolated ``retrieval_2`` pipeline.

Normal run (exact/FTS retrieval, no model download)::

    python test/test5.py

Optional semantic fallback through the existing LanceDB index::

    python test/test5.py --semantic

This script only reads the existing index.  It does not rebuild or modify it.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from base.index_d import BranchAndDir
from retrieval_2 import GrepFirstRetrievalPipeline, RetrievalOptions
from utils.paths import get_index_sqlite_path


DB_PATH = get_index_sqlite_path()
WORKSPACE_DIR = Path.cwd()
TOP_K = 15

# These deliberately mix exact symbol names, code literals, paths, and prose.
QUERIES = [
    ("symbol-class", "ChunkCodebaseIndex", "auto"),
    ("symbol-method", "insert_chunks", "auto"),
    ("literal", "parentId", "grep"),
    ("path", "fullTextSearchCodebaseIndex.py", "grep"),
    ("prose", "where is the FTS index updated", "auto"),
    ("prose-2", "how are chunks stored in sqlite", "auto"),
    ("prose-3", "how is retrival pipiline is working", "auto"),
    ("prose-4", "how code snippets are being stored to db", "auto"),
    ("prose-5", "how is pipeline.py is working ", "auto"),
    ("prose-6", "how dfs walker is navigating and discovering the files in the repo", "auto"),
    ("prose-7", "how soes watcher keep the track of recently changes files", "auto"),
    ("prose-8", "what is tree-sitter queries for??", "auto"),
    ("prose-9", "how indexing pipeline works and where is it entry point ??", "auto"),
    ("prose-10", "explain models.py in base folder", "auto"),
]


def _short_path(path: str, width: int = 70) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1) :]


def _print_hit(index: int, hit) -> None:
    sources = ",".join(sorted(hit.sources or []))
    symbol = hit.symbol_name or "-"
    occurrences = ", ".join(
        f"L{o.line}:C{o.column_start}-{o.column_end}" for o in hit.occurrences
    )
    occurrences = occurrences or "-"
    print(
        f"  {index:<4} {hit.match_kind:<16} {hit.score:<9.4f} "
        f"{symbol:<28} {hit.start_line:>5}-{hit.end_line:<5} "
        f"{sources:<34} {_short_path(hit.path)}"
    )
    print(f"       matches: {occurrences}")
    preview = " ".join(hit.content.strip().split())
    if len(preview) > 180:
        preview = preview[:177] + "..."
    print(f"       text:    {preview}")


def _print_context(result) -> None:
    if not result.context:
        print("\n  Context: none")
        return

    print(f"\n  Context groups: {len(result.context)}")
    for group_index, group in enumerate(result.context, 1):
        print(
            f"  [{group_index}] {group.symbol_type or '-'} "
            f"{group.symbol_name or '-'} "
            f"({group.path}:{group.start_line}-{group.end_line}) "
            f"sources={','.join(group.sources) or '-'}"
        )
        for chunk in group.chunks:
            print(
                f"       chunk {chunk.chunk_id} "
                f"piece={chunk.piece_index + 1}/{chunk.piece_count} "
                f"symbol={chunk.symbol_name or '-'} "
                f"lines={chunk.start_line}-{chunk.end_line} "
                f"source={chunk.source}"
            )


async def _create_optional_lance(db, workspace_dirs):
    """Create the existing LanceDB adapter only when --semantic is requested."""

    from embeddings.local import LocalEmbeddings
    from lance_db.lanceDbIndex import LanceDbIndex
    from utils.disk_operations import DiskOperations

    print("[semantic] Loading local embedding model ...")
    embeddings = LocalEmbeddings("jinaai/jina-embeddings-v2-base-code")
    filesystem = DiskOperations(roots=list(workspace_dirs))
    lance = await LanceDbIndex.create(
        db=db,
        embeddings_provider=embeddings,
        filesystem=filesystem,
    )
    if lance is None:
        raise RuntimeError("Could not initialize LanceDB")
    print(f"[semantic] Embedding model: {embeddings.embedding_id}")
    return lance


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="enable the existing LanceDB/vector fallback",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=TOP_K,
        help=f"number of evidence hits per query (default: {TOP_K})",
    )
    args = parser.parse_args()

    print(f"[1/4] Opening SQLite DB: {DB_PATH}")
    if not DB_PATH.exists():
        print("  !! Database not found. Run the indexing pipeline first.")
        return

    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"  !! Could not open SQLite DB: {exc}")
        print("     Check that the index path is readable by this process.")
        return
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")

        tables = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        print(f"  !! Could not read SQLite DB: {exc}")
        print("     Check that the index file and any required sidecars are readable.")
        db.close()
        return
    print(f"  Tables found: {', '.join(sorted(tables))}")
    required = {"chunks", "symbols", "chunk_tags"}
    missing = required - tables
    if missing:
        print(f"  !! Missing required tables: {sorted(missing)}")
        db.close()
        return

    print("[2/4] Resolving workspace scope ...")
    # Indexing uses file:// workspace URIs.  Reusing DiskOperations here keeps
    # the query tags identical to the tags written by the indexer.
    from utils.disk_operations import DiskOperations
    from retrieval.utils import get_current_tags

    filesystem = DiskOperations(roots=[str(WORKSPACE_DIR.resolve())])
    workspace_dirs = await filesystem.get_workspace_dirs()
    tags: list[BranchAndDir] = get_current_tags(workspace_dirs)
    print(f"  Workspace dirs: {workspace_dirs}")
    print(f"  Tags: {tags}")

    print("[3/4] Building grep-first retrieval pipeline ...")
    lance = None
    if args.semantic:
        lance = await _create_optional_lance(db, workspace_dirs)
    else:
        print("  Vector fallback disabled (use --semantic to enable it).")

    pipeline = GrepFirstRetrievalPipeline(
        db,
        lance_index=lance,
        root_directory=workspace_dirs,
    )

    print(f"[4/4] Running {len(QUERIES)} queries ...")
    for label, query, mode in QUERIES:
        print("\n" + "=" * 110)
        print(f"[{label}] mode={mode} query={query!r}")
        print("=" * 110)

        result = await pipeline.retrieve(
            query,
            tags=tags,
            options=RetrievalOptions(
                mode=mode,
                top_k=max(0, args.top_k),
                max_context_symbols=10,
            ),
        )

        print(
            f"  hits={len(result.hits)} "
            f"exact={result.exact_hit_count} "
            f"fts={result.lexical_hit_count} "
            f"vector_used={result.vector_used}"
        )
        if result.warnings:
            for warning in result.warnings:
                print(f"  warning: {warning}")

        if result.hits:
            print(
                f"\n  {'RANK':<4} {'KIND':<16} {'SCORE':<9} "
                f"{'SYMBOL':<28} {'LINES':<12} {'SOURCES':<34} PATH"
            )
            print("  " + "-" * 106)
            for index, hit in enumerate(result.hits, 1):
                _print_hit(index, hit)
        else:
            print("\n  No hits.")

        _print_context(result)
    db.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Diagnostic harness for the lookup-only ``retrieval_2`` pipeline.

Run from the repository root after indexing::

    python retrieval_2/test_pipeline.py
    python retrieval_2/test_pipeline.py insert_chunks Widget

The script opens the existing index read-only, prints every matching symbol,
and prints reconstructed code for each match. It does not use the FTS or
vector indexes.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from src.coreindexer.retrieval import SymbolCode, SymbolLookup
from src.coreindexer.utils.paths import get_index_sqlite_path
from src.coreindexer.utils.disk_operations import DiskOperations



REQUIRED_TABLES = {"symbols", "chunks"}


def _short_path(path: str, width: int = 72) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1) :]


def _print_code_tree(node: SymbolCode, indent: str = "  ") -> None:
    # print(
    #     # f"  {indent}node_type: {node.type} \n    node_name: {node.name} "
    #     # f"\n    path: ({node.path}:{node.start_line}-{node.end_line})"
    # )
    print(f"{indent}  pieces: {len(node.pieces)}")
    if node.code:
        # print(f"{indent}\n    code:")
        print("   ", node.code)
        # for line in node.code.splitlines() or [node.code]:
        #     print(f"{indent}    {line}")
    else:
        print(f"{indent}\n    code: <no stored chunks>")

    for child in node.children:
        _print_code_tree(child, indent + "  ")


def _print_matches(result) -> None:
    print(f"match kind: {result.match_kind or 'none'}")
    if not result.matches:
        print("matches: none")
        return

    print(f"matches: {len(result.matches)}")
    for index, match in enumerate(result.matches, 1):
        print(
            f"\n[{index}] {match.match_kind:<9} \n    id: {match.id} "
            f"\n    type: {match.type:<10} \n    name: {match.name} "
            f"\n    path: ({match.path}:{match.start_line}-{match.end_line})"
        )


DB_PATH = get_index_sqlite_path()
WORKSPACE_DIR = Path.cwd()
QUERYS = [
    "dfswalker", 
    "chunkcodebaseIndex",
    "_insert_or_raise",
    "SYMBOLLOOKUP",
    "sound",
    "update",
    "main",
    "construct_class_definition_chunk",
    "collapse_children",
    "collect_ids"
    
    # another repo symbols
    
    # "RetrievalPipelineOptions",
    # "BaseRetrievalPipeline",
    # "retrieveContextItemsFromEmbeddings",
    # "HttpContextProvider",
    # "updateIndexAndAwaitGenerator",
    # "ChunkCodebaseIndex",
    # "createMemoryRouter",
    # "App"
    
]

async def main() -> None:

    print(f"[1/3] Opening SQLite DB at {DB_PATH} ...")
    if not DB_PATH.exists():
        print(f"  !! DB not found at {DB_PATH}. UpdateDB_PATH in the script.")
        return
   
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    
    # FileSystem
    fs = DiskOperations(roots=[str(WORKSPACE_DIR.resolve())])
    roots = (await fs.get_workspace_dirs())
    
    try:
        tables = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        print(f"  tables found: {', '.join(sorted(tables))}")
        missing = REQUIRED_TABLES - tables
        if missing:
            print(f"  !! Missing required tables: {sorted(missing)}")
            return

        print("[2/3] Building SymbolLookup")
        lookup = SymbolLookup(db, roots)
        print(f"[3/3] Running symbol lookups\n")

        for index, name in enumerate(QUERYS):
            print("=" * 88)
            print(f"[{index}] {name!r}")
            print("=" * 88)

            result = lookup.lookup(
                name, 
                # detail="signature"
            )
            _print_matches(result)

            # A single match is selected automatically by the API. With
            # duplicates, reconstruct each row to make selection explicit.
            if result.selected is not None:
                print("    selected: unique match\n    code:\n")
                _print_code_tree(result.selected)
            else:
                for match in result.matches:
                    print(f"\n  selected explicitly: {match.id}\n  code:\n")
                    _print_code_tree(
                        lookup.reconstruct(
                            match.id,
                            # detail="signature"
                        )
                    )
            print()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

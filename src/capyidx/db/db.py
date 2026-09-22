from __future__ import annotations

import sqlite3
import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from capyidx.db import paths
from capyidx.base.index_d import BranchAndDir
from capyidx.db.schema import init_schema


def open_index(tags: BranchAndDir, *, db_path: Path | None = None) -> sqlite3.Connection:
    """Open the default index, or one at an explicit path.

    The returned connection is fully initialized (WAL, foreign keys, schema).
    Caller owns the connection lifetime.
    """
    if db_path is None:
        db_path = paths.db_path_for(tags)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _apply_pragmas(conn)
    init_schema(conn)

    _write_metadata_once(tags, db_path)
    return conn


def attach_index(
    conn: sqlite3.Connection,
    *,
    schema: str = "index1",
    path: Path | None = None,
    tags=None,
) -> None:
    """Attach a second index DB to an existing connection.

    After this, queries can reference `index1.symbols`, `index1.chunks`, etc.
    Use when you want to query across two indexes without a second connection.
    """
    if path is None:
        if tags is None:
            raise ValueError("attach_index requires either path= or tags=")
        path = paths.db_path_for(tags)

    path.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("ATTACH DATABASE ? AS " + schema, (str(path),))
    init_schema(conn)   # ATTACH puts the new DB in scope, so DDL lands there


@contextmanager
def using_index(
    tags: BranchAndDir,
    *,
    db_path: Path | None = None,
    in_memory: bool = False,
) -> Generator[sqlite3.Connection]:
    """Context manager form: closes the connection for you."""
    if in_memory:
        conn = sqlite3.connect(":memory:")
        _apply_pragmas(conn)
        init_schema(conn)
        try:
            yield conn
        finally:
            conn.close()
        return

    conn = open_index(tags, db_path=db_path)
    try:
        yield conn
    finally:
        conn.close()


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA foreign_keys=ON")


def _write_metadata_once(tags, db_path: Path) -> None:
    if tags is None:
        return
    meta = paths.metadata_path_for(tags)
    if meta.exists():
        return
    meta.parent.mkdir(parents=True, exist_ok=True)
    
    meta.write_text(
        json.dumps(
            {
                "directory": tags.directory,
                "branch": tags.branch,
                "sqlite_filename": db_path.name,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
         
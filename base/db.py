from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, List

from utils.paths import get_coreIndexer_global_path
from utils.retrieval_utils import get_current_tags
from base.index_d import BranchAndDir
import hashlib
import json

class SqliteDB:
    _db: Optional[sqlite3.Connection] = None
    _db_path: Optional[Path] = None
    
    def __init__(
        self,
        workspace_tag: BranchAndDir
    ) -> None:
        global _tags
        _tags = workspace_tag

    
    @staticmethod
    def default_path() -> Path:
        
        index_folder = get_coreIndexer_global_path() / ".codebase_index"
        
        raw_identifier = f"{_tags.directory}__{_tags.branch}"
        
        db_hash = hashlib.sha256(raw_identifier.encode("utf-8")).hexdigest()
        
        sqlite_index_file = index_folder / f"index__{db_hash}.sqlite"
        
        metadata_path = get_coreIndexer_global_path() / ".codebase_index"  / f"index__{db_hash}.metadata.json"
        
        metadata_payload = {
            "hash": db_hash,
            "directory": _tags.directory,
            "branch": _tags.branch,
            "sqlite_filename": sqlite_index_file.name
        }
        try:
            metadata_path.write_text(json.dumps(metadata_payload, indent=2), encoding="utf-8")
        except OSError as e:
            # Gracefully log metadata failure without breaking core database creation
            print(f"Warning: Could not write tracking metadata file: {e}")
            
        return sqlite_index_file

    @classmethod
    def initialize(cls, db_path: Optional[Path] = None) -> None:
        """
        Run once during application startup.
        Creates tables, indexes and cleans duplicate rows.
        """
        path = Path(db_path) if db_path else cls.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        db = sqlite3.connect(str(path))

        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout = 3000;")
        db.execute("PRAGMA foreign_keys = ON")
        

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tag_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dir TEXT NOT NULL,
                branch TEXT NOT NULL,
                artifactId TEXT NOT NULL,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                lastUpdated INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS global_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cacheKey TEXT NOT NULL,
                dir TEXT NOT NULL,
                branch TEXT NOT NULL,
                artifactId TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS indexing_lock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                locked INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                dirs TEXT NOT NULL
            );

            DELETE FROM tag_catalog
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM tag_catalog
                GROUP BY dir, branch, artifactId, path, cacheKey
            );

            DELETE FROM global_cache
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM global_cache
                GROUP BY cacheKey, dir, branch, artifactId
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_catalog_unique
                ON tag_catalog(dir, branch, artifactId, path, cacheKey);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_global_cache_unique
                ON global_cache(cacheKey, dir, branch, artifactId);
            """
        )

        db.commit()
        db.close()

    @classmethod
    def get(cls, db_path: Optional[Path] = None) -> sqlite3.Connection:
        """
        Runtime connection only.
        No schema creation happens here.
        """
        path = Path(db_path) if db_path else cls.default_path()

        if cls._db is not None and cls._db_path == path:
            return cls._db

        cls._db_path = path
        cls._db = sqlite3.connect(str(path), check_same_thread=False)
        cls._db.row_factory = sqlite3.Row
        cls._db.execute("PRAGMA busy_timeout = 3000;")

        return cls._db

    @classmethod
    def close(cls) -> None:
        if cls._db is not None:
            cls._db.close()
            cls._db = None
            cls._db_path = None
            
            
            
# from __future__ import annotations
# import sqlite3
# import json
# from contextlib import contextmanager
# from pathlib import Path
# from typing import Iterator

# from utils import paths
# from base.schema import init_schema


# def open_index(tags, *, db_path: Path | None = None) -> sqlite3.Connection:
#     """Open the default index, or one at an explicit path.

#     The returned connection is fully initialized (WAL, foreign keys, schema).
#     Caller owns the connection lifetime.
#     """
#     if db_path is None:
#         db_path = paths.db_path_for(tags)

#     db_path.parent.mkdir(parents=True, exist_ok=True)
#     conn = sqlite3.connect(str(db_path), check_same_thread=False)
#     _apply_pragmas(conn)
#     init_schema(conn)

#     _write_metadata_once(tags, db_path)
#     return conn


# def attach_index(
#     conn: sqlite3.Connection,
#     *,
#     schema: str = "index1",
#     path: Path | None = None,
#     tags=None,
# ) -> None:
#     """Attach a second index DB to an existing connection.

#     After this, queries can reference `index1.symbols`, `index1.chunks`, etc.
#     Use when you want to query across two indexes without a second connection.
#     """
#     if path is None:
#         if tags is None:
#             raise ValueError("attach_index requires either path= or tags=")
#         path = paths.db_path_for(tags)

#     path.parent.mkdir(parents=True, exist_ok=True)
#     conn.execute("ATTACH DATABASE ? AS " + schema, (str(path),))
#     init_schema(conn)   # ATTACH puts the new DB in scope, so DDL lands there


# @contextmanager
# def using_index(
#     tags=None,
#     *,
#     db_path: Path | None = None,
#     in_memory: bool = False,
# ) -> Iterator[sqlite3.Connection]:
#     """Context manager form: closes the connection for you."""
#     if in_memory:
#         conn = sqlite3.connect(":memory:")
#         _apply_pragmas(conn)
#         init_schema(conn)
#         try:
#             yield conn
#         finally:
#             conn.close()
#         return

#     conn = open_index(tags, db_path=db_path)
#     try:
#         yield conn
#     finally:
#         conn.close()


# def _apply_pragmas(conn: sqlite3.Connection) -> None:
#     conn.row_factory = sqlite3.Row
#     conn.execute("PRAGMA journal_mode=WAL")
#     conn.execute("PRAGMA synchronous=NORMAL")
#     conn.execute("PRAGMA busy_timeout=3000")
#     conn.execute("PRAGMA foreign_keys=ON")


# def _write_metadata_once(tags, db_path: Path) -> None:
#     if tags is None:
#         return
#     meta = paths.metadata_path_for(tags)
#     if meta.exists():
#         return
#     meta.parent.mkdir(parents=True, exist_ok=True)
    
#     meta.write_text(
#         json.dumps(
#             {
#                 "directory": tags.directory,
#                 "branch": tags.branch,
#                 "sqlite_filename": db_path.name,
#             },
#             indent=2,
#         ),
#         encoding="utf-8",
#     )
         
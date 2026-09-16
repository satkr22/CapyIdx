from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from utils.paths import get_coreIndexer_global_path

class SqliteDB:
    _db: Optional[sqlite3.Connection] = None
    _db_path: Optional[Path] = None

    @staticmethod
    def default_path() -> Path:
        
        return get_coreIndexer_global_path() / ".codebase_index" / "index.sqlite"

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
        db.execute("PRAGMA busy_timeout = 3000;")

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
            
            
hi = "hii"
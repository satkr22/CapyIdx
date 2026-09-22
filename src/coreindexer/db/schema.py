from __future__ import annotations
import sqlite3

SCHEMA_VERSION = 1

_DDL = """
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



def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def is_index_db(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols'"
    ).fetchone()
    return row is not None
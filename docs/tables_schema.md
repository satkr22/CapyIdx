chunk_tags : CREATE TABLE chunk_tags(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                tag      TEXT NOT NULL,
                chunkId  TEXT NOT NULL,
                FOREIGN KEY(chunkId) REFERENCES chunks(id) ON DELETE CASCADE,
                UNIQUE(tag, chunkId)
            )
chunks : CREATE TABLE chunks(
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
            )
code_snippets : CREATE TABLE code_snippets (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                content TEXT NOT NULL,
                title TEXT NOT NULL,
                signature TEXT,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL
            )
code_snippets_tags : CREATE TABLE code_snippets_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                snippetId INTEGER NOT NULL,
                FOREIGN KEY (snippetId) REFERENCES code_snippets (id)
            )
fts : CREATE VIRTUAL TABLE fts USING fts5(
                path,
                content,
                tokenize = 'trigram'
            )
fts_config : CREATE TABLE 'fts_config'(k PRIMARY KEY, v) WITHOUT ROWID
fts_content : CREATE TABLE 'fts_content'(id INTEGER PRIMARY KEY, c0, c1)
fts_data : CREATE TABLE 'fts_data'(id INTEGER PRIMARY KEY, block BLOB)
fts_docsize : CREATE TABLE 'fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)
fts_idx : CREATE TABLE 'fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID
fts_metadata : CREATE TABLE fts_metadata (
                id        INTEGER PRIMARY KEY,   -- = fts.rowid (soft link, no FK)
                path      TEXT    NOT NULL,
                cacheKey  TEXT    NOT NULL,
                chunkId   TEXT    NOT NULL,      -- chunks.id is a UUID string
                FOREIGN KEY (chunkId) REFERENCES chunks (id) ON DELETE CASCADE
            )
global_cache : CREATE TABLE global_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cacheKey TEXT NOT NULL,
                dir TEXT NOT NULL,
                branch TEXT NOT NULL,
                artifactId TEXT NOT NULL
            )
indexing_lock : CREATE TABLE indexing_lock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                locked INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                dirs TEXT NOT NULL
            )
lance_db_cache : CREATE TABLE lance_db_cache (
                uuid TEXT PRIMARY KEY,
                cacheKey TEXT NOT NULL,
                path TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                vector TEXT NOT NULL,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL,
                contents TEXT NOT NULL
            )
symbols : CREATE TABLE symbols(
                id         TEXT PRIMARY KEY,
                type       TEXT NOT NULL,
                name       TEXT NOT NULL,
                parentId   TEXT,
                startLine  INTEGER NOT NULL,
                endLine    INTEGER NOT NULL,
                cacheKey   TEXT NOT NULL,
                path       TEXT NOT NULL,
                FOREIGN KEY(parentId) REFERENCES symbols(id) ON DELETE CASCADE
            )
tag_catalog : CREATE TABLE tag_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dir TEXT NOT NULL,
                branch TEXT NOT NULL,
                artifactId TEXT NOT NULL,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                lastUpdated INTEGER NOT NULL
            )

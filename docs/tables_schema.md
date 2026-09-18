sqlite> .tables
chunk_tags          fts                 fts_docsize         indexing_lock
chunks              fts_config          fts_idx             lance_db_cache
code_snippets       fts_content         fts_metadata        tag_catalog
code_snippets_tags  fts_data            global_cache
sqlite> .schema chunk_tags
CREATE TABLE chunk_tags(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                chunkId INTEGER NOT NULL,
                FOREIGN KEY(chunkId) REFERENCES chunks(id),
                UNIQUE(tag, chunkId)
            );
sqlite> .header on
sqlite> .mode column
sqlite> .schema chunk_tags
CREATE TABLE chunk_tags(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                chunkId INTEGER NOT NULL,
                FOREIGN KEY(chunkId) REFERENCES chunks(id),
                UNIQUE(tag, chunkId)
            );
sqlite> .schema
CREATE TABLE tag_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dir TEXT NOT NULL,
                branch TEXT NOT NULL,
                artifactId TEXT NOT NULL,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                lastUpdated INTEGER NOT NULL
            );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE global_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cacheKey TEXT NOT NULL,
                dir TEXT NOT NULL,
                branch TEXT NOT NULL,
                artifactId TEXT NOT NULL
            );
CREATE TABLE indexing_lock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                locked INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                dirs TEXT NOT NULL
            );
CREATE UNIQUE INDEX idx_tag_catalog_unique
                ON tag_catalog(dir, branch, artifactId, path, cacheKey);
CREATE UNIQUE INDEX idx_global_cache_unique
                ON global_cache(cacheKey, dir, branch, artifactId);
CREATE TABLE chunks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cacheKey TEXT NOT NULL,
                path TEXT NOT NULL,
                idx INTEGER NOT NULL,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL,
                content TEXT NOT NULL
            );
CREATE TABLE chunk_tags(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                chunkId INTEGER NOT NULL,
                FOREIGN KEY(chunkId) REFERENCES chunks(id),
                UNIQUE(tag, chunkId)
            );
CREATE VIRTUAL TABLE fts USING fts5(
                path,
                content,
                tokenize = 'trigram'
            )
/* fts(path,content) */;
CREATE TABLE IF NOT EXISTS 'fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE IF NOT EXISTS 'fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'fts_content'(id INTEGER PRIMARY KEY, c0, c1);
CREATE TABLE IF NOT EXISTS 'fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE fts_metadata (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                chunkId INTEGER NOT NULL,
                FOREIGN KEY (chunkId) REFERENCES chunks (id),
                FOREIGN KEY (id) REFERENCES fts (rowid)
            );
CREATE TABLE code_snippets (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                content TEXT NOT NULL,
                title TEXT NOT NULL,
                signature TEXT,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL
            );
CREATE TABLE code_snippets_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                snippetId INTEGER NOT NULL,
                FOREIGN KEY (snippetId) REFERENCES code_snippets (id)
            );
CREATE UNIQUE INDEX idx_code_snippets_unique
                ON code_snippets (path, cacheKey, content, title, startLine, endLine)
                ;
CREATE UNIQUE INDEX idx_snippetId_tag
                ON code_snippets_tags (snippetId, tag)
                ;
CREATE TABLE lance_db_cache (
                uuid TEXT PRIMARY KEY,
                cacheKey TEXT NOT NULL,
                path TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                vector TEXT NOT NULL,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL,
                contents TEXT NOT NULL
            );
sqlite>
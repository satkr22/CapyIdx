import sqlite3
from pathlib import Path

DB_PATH = "/home/usatkr/.coreIndexer/.codebase_index/index.sqlite"

conn = sqlite3.connect(Path(DB_PATH))
db = conn.cursor()
db.row_factory = sqlite3.Row

# db.execute("SELECT sql FROM sqlite_master WHERE type='table'")
# print(db.fetchall())

# db.execute("PRAGMA schema_version")
# db.execute("SELECT name AS table_name, sql AS schema FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
# for row in db.fetchall():
#     print(row["table_name"], ":", row["schema"])

# db.execute("select * from chunks limit 4")
# db.execute("select * from chunks where path like '%lanceDbIndex.py%' order by startLine")

# for row in db.fetchall():
#     print(row["id"])
#     print(row["cacheKey"])
#     print(row["path"])
#     print(row["idx"])
#     print(row["startLine"])
#     print(row["endLine"])
#     print(row["content"])
#     print("-"*70)
#     print("\n\n")
    
# db.execute("select * from fts_metadata where chunkId = 159")
db.execute("select id, path, content from code_snippets order by id")
# db.execute("select * from code_snippets where path like '%lanceDbIndex.py'")
# print(db.fetchone())
for col in db.fetchall():
    for x in col:
        print(x)
    print("\n")
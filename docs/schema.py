import sqlite3
from pathlib import Path

DB_PATH = "/home/usatkr/.coreIndexer/.codebase_index/index2.sqlite"

conn = sqlite3.connect(Path(DB_PATH))
db = conn.cursor()
db.row_factory = sqlite3.Row

# db.execute("SELECT sql FROM sqlite_master WHERE type='table'")
# print(db.fetchall())

# db.execute("PRAGMA schema_version")


db.execute("SELECT name AS table_name, sql AS schema FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
for row in db.fetchall():
    print(row["table_name"], ":", row["schema"])
    
    
    
# for x in db.fetchall():
#     for y in x:
#         print(y)
#     print("\n")
# print(db.fetchall())

# db.execute("select * from chunks limit 4")
# db.execute("select * from chunks where path like '%lanceDbIndex.py%' order by startLine")
# db.execute("select * from chunks")

# for row in db.fetchall():
#     for col in row:
#         print(col)
#     print("\n")
    
# db.execute("select * from fts_metadata where chunkId = 159")
# db.execute("select count(*) from code_snippets")
# db.execute("select id, path, content from code_snippets order by id")
# db.execute("select * from code_snippets where path like '%lanceDbIndex.py'")
# print(db.fetchall()[0])

# db.execute("select * from fts")
# for col in db.fetchall():
#     for x in col:
#         print(x)
#     print("\n")



# db.execute("select * from symbols")
# db.execute("select * from code_snippets")

# for row in db.fetchall():
#     for col in row:
#         print(col)
#     print("\n")
import sqlite3
from pathlib import Path

DB_PATH = "/home/usatkr/.coreIndexer/.codebase_index/index.sqlite"

conn = sqlite3.connect(Path(DB_PATH))
db = conn.cursor()
db.row_factory = sqlite3.Row

# db.execute("SELECT sql FROM sqlite_master WHERE type='table'")
# print(db.fetchall())

# db.execute("PRAGMA schema_version")
db.execute("SELECT name AS table_name, sql AS schema FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
for row in db.fetchall():
    print(row["table_name"], ":", row["schema"])


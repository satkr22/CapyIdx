# fs = DiskOperations()

# indexer = CodeIndexer(
#     fs=fs,
#     indexes=[
#         ChunkCodebaseIndex(...),
#         FullTextSearchCodebaseIndex(...),
#         LanceDbIndex(...),
#         CodeSnippetsCodebaseIndex(...),
#     ],
# )

# # Initial indexing
# async for _ in indexer.refresh_codebase_index(
#     await fs.get_workspace_dirs()
# ):
#     pass

# # Continuous indexing
# async for update in indexer.start_watch(
#     await fs.get_workspace_dirs()
# ):
#     print(update.desc)



from __future__ import annotations

import asyncio
from pathlib import Path
from time import perf_counter

from base.db import SqliteDB
from utils.disk_operations import DiskOperations
from chunker.chunkCodebaseIndex import ChunkCodebaseIndex
from fts.fullTextSearchCodebaseIndex import FullTextSearchCodebaseIndex
from codesnippet.codeSnippetsIndex import CodeSnippetsCodebaseIndex
from lance_db.lanceDbIndex import LanceDbIndex
from embeddings.local import LocalEmbeddings
from indexer.codeBaseIndexer import CodeIndexer


# Change this to the repository you want to index
WORKSPACE = Path("/home/usatkr/u_ml/projects/continue_fork").resolve()
# WORKSPACE = Path("/home/usatkr/u_ml/projects/AI_Copilot").resolve()
# WORKSPACE = Path.cwd()


async def main():
    
    # Embeddings
    # embeddings_provider = LocalEmbeddings()
    # embeddings_provider = LocalEmbeddings("jinaai/jina-embeddings-v2-base-code")
    
    # Build indexes (only Chunk index for now)    
    # fts_index = FullTextSearchCodebaseIndex(
    #     db=db
    # )
    
    # code_snippets_index = CodeSnippetsCodebaseIndex(
    #     filesystem=fs,
    #     db=db
    # )
    
    # lancedb_index = await LanceDbIndex.create(
    #     db=db,
    #     embeddings_provider=embeddings_provider,
    #     filesystem=fs
    # )
    # if lancedb_index is None:
    #     raise RuntimeError("Failed to create LanceDB index")
    
    
    #----------------------------------------------------------
    
    
    
    # Initialise database
    SqliteDB.initialize()
    db = SqliteDB.get()

    # Filesystem
    fs = DiskOperations(roots=[str(WORKSPACE.resolve())])
    root = (await fs.get_workspace_dirs())
    
    # chunker initialization
    chunk_index = ChunkCodebaseIndex(
        db=db,
        filesystem=fs,
        # max_chunk_size=embeddings_providermax_embedding_chunk_size,
        max_chunk_size=512
    )
    
    # main orchestrator
    indexer = CodeIndexer(
        fs=fs,
        indexes=[
            chunk_index, 
            # fts_index, 
            # code_snippets_index,
            # lancedb_index
        ],
    )
    
    # Run indexing
    async for update in indexer.refresh_codebase_index(root):
        print(
            f"[{update.status.upper():9}] "
            f"{update.progress:6.1%} | {update.desc}"
        )
        if update.warnings:
            for warning in update.warnings:
                print("   Warning:", warning)
    print("\nIndexing finished.")
    
    
    print("read_time:", chunk_index.read_time)
    print("parse_time:", chunk_index.parse_time)
    with open("res.txt", "w+") as f:
        for t in chunk_index.parse_time:
            f.write(f"{t}\n")
        
    print("chunk_time:", chunk_index.chunk_time)
    print("db_time:", chunk_index.db_time)


    # Verify database contents
    print("\n--- Database Stats ---")
    chunk_count = db.execute(
        "SELECT COUNT(*) FROM chunks"
    ).fetchone()[0]
    tag_count = db.execute(
        "SELECT COUNT(*) FROM chunk_tags"
    ).fetchone()[0]
    catalog_count = db.execute(
        "SELECT COUNT(*) FROM tag_catalog"
    ).fetchone()[0]
    print(f"Chunks      : {chunk_count}")
    print(f"Chunk Tags  : {tag_count}")
    print(f"Tag Catalog : {catalog_count}")
    print("\nSample chunks:\n")
    rows = db.execute(
        """
        SELECT path, idx, startLine, endLine
        FROM chunks
        ORDER BY path, idx
        LIMIT 10
        """
    ).fetchall()
    for row in rows:
        print(
            f"{Path(row[0]).name:25} "
            f"chunk={row[1]:2} "
            f"lines={row[2]}-{row[3]}"
        )


if __name__ == "__main__":
    asyncio.run(main())
import asyncio
from pathlib import Path

from src.coreindexer.db.db import SqliteDB
from src.coreindexer.utils.disk_operations import DiskOperations
from src.coreindexer.indexer.codebase_indexer import CodeIndexer
from coreindexer.chunker.chunk_codebase_index import ChunkCodebaseIndex
from src.coreindexer.fts.fulltextsearch_codebase_index import FullTextSearchCodebaseIndex
from src.coreindexer.codesnippet.code_snippets_index import CodeSnippetsCodebaseIndex
from src.coreindexer.lance_db.lancedb_index import LanceDbIndex
from src.coreindexer.embeddings.local import LocalEmbeddings
from src.coreindexer.base.index_d import BranchAndDir
from src.coreindexer.utils.retrieval_utils import get_current_tags

# WORKSPACE = Path("/home/usatkr/u_ml/projects/AI_Copilot").resolve()
# WORKSPACE = Path("/home/usatkr/u_ml/projects/continue_fork").resolve()

WORKSPACE = Path.cwd()



async def main():
    
    fs = DiskOperations(roots=[str(WORKSPACE)])
    root = (await fs.get_workspace_dirs())
    
    workspace_tag = get_current_tags(root)[0]
    
    
    SqliteDB(workspace_tag).initialize()
    db = SqliteDB.get()
    # emb = LocalEmbeddings("jinaai/jina-embeddings-v2-base-code")

    chunk_index = ChunkCodebaseIndex(
        db=db, 
        filesystem=fs, 
        max_chunk_size=512
        # max_chunk_size=emb.max_embedding_chunk_size
    )
    # fts_index = FullTextSearchCodebaseIndex(db=db)
    # snippets_index = CodeSnippetsCodebaseIndex(filesystem=fs, db=db)
    # lance_index = await LanceDbIndex.create(db=db, embeddings_provider=emb, filesystem=fs)
    # if lance_index is None:
    #     raise RuntimeError("Failed to create LanceDB index")
    
    indexer = CodeIndexer(
        fs=fs,
        indexes=[
            chunk_index, 
            # fts_index, 
            # snippets_index, 
            # lance_index
        ],
    )

    # 1) Initial full index
    async for update in indexer.refresh_codebase_index(root):
        print(f"[{update.status}] {update.progress:.1%} {update.desc}")

    # 2) Continuous re-index of changed files
    #    workspace_dirs MUST be the same strings you passed to refresh_codebase_index
    watch_task = asyncio.create_task(_consume(indexer, root))

    try:
        stop_event = asyncio.Event()
        # await asyncio.sleep(3600)  # or until shutdown signal
        await stop_event.wait() 
        
    finally:
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass

async def _consume(indexer, workspace_dirs):
    async for update in indexer.start_watch(workspace_dirs, flush_interval=5):
        print(f"[watch] {update.progress:.1%} {update.desc}")

if __name__ == "__main__":
    asyncio.run(main())
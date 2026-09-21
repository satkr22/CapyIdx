# test_reranker.py
"""
Reranker pipeline test harness.

Runs the same query set through:
  1. RRF only              (NoRerankerRetrievalPipeline)
  2. RRF + reranker        (RerankerRetrievalPipeline with CrossEncoderReranker)
  3. RRF + mock reranker   (a deterministic fake so you can verify plumbing
                            without downloading a model)

Prints ranked ContextItems with the delta vs. RRF, so you can see what moved.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

from base.index_d import BranchAndDir                      
from fts.fullTextSearchCodebaseIndex import FullTextSearchCodebaseIndex  
from lance_db.lanceDbIndex import LanceDbIndex                           
from retrieval.no_rerank_pipeline import NoRerankerRetrievalPipeline     
from retrieval.rerank_pipeline import RerankerRetrievalPipeline        
from retrieval.rerankers import BaseReranker, CrossEncoderReranker       
from utils.paths import get_index_sqlite_path
from utils.disk_operations import DiskOperations
from embeddings.local import LocalEmbeddings
from retrieval.utils import get_current_tags


DB_PATH = get_index_sqlite_path()         
WORKSPACE_DIR = Path.cwd()                       
QUERIES = [
    ("res1", "Explain how chunkCodebaseIndex work?"),
    ("res2", "how is retrieval working ??"),
    ("res3", "how are chunks stored in sqlite"),
    ("res4", "where does the FTS index get updated"),
    ("res5", "how does hybrid scoring work"),
    ("res6", "where read_file is implemented?"),
    ("res7", "where filesystem operations are implemented?"),
]
TOP_K = 15


# ---------------------------------------------------------------------------
# Mock reranker -- deterministic, no model download, no network.
# Scores by simple term overlap. If your plumbing works, this should visibly
# reorder results (e.g. chunks that literally mention the query terms rise).
# ---------------------------------------------------------------------------
class MockReranker(BaseReranker):
    async def score(self, query: str, passages: list[str]) -> list[float]:
        terms = {t for t in query.lower().split() if len(t) >= 3}
        out = []
        for p in passages:
            p_lower = (p or "").lower()
            hits = sum(1 for t in terms if t in p_lower)
            # tiny length penalty so identical-hit passages prefer the shorter one
            out.append(hits / max(1, len(p_lower)) ** 0.25)
        return out


async def main():
    
    
    print(f"[1/7] Opening SQLite DB at {DB_PATH} ...")
    if not DB_PATH.exists():
        print(f"  !! DB not found at {DB_PATH}. UpdateDB_PATH in the script.")
        return
    
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    
    # Sanity check: what tables exist?
    tables = [r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' OR type='view'"
    ).fetchall()]
    print(f"  Tables found: {sorted(tables)}")

    required_tables = {"chunks", "symbols", "fts_metadata","chunk_tags", "lance_db_cache"}
    missing = required_tables - set(tables)
    if missing:
        print(f"  !! Missing required tables: {missing}")
        print("     Did you run the indexing pipeline first?")
        return
    # ---- 2. Instantiate embeddings provider ----
    
    print("[2/7] Initializing embeddings provider ...")
    # Adjust the constructor args to match YOUR Embeddingsclass.
    embeddings_provider = LocalEmbeddings("jinaai/jina-embeddings-v2-base-code")
    print(f"  Embedding model: {embeddings_provider.embedding_id}")
    
    
    # FileSystem
    fs = DiskOperations(roots=[str(WORKSPACE_DIR.resolve())])
    root = (await fs.get_workspace_dirs())
    # print("root------:", root)
        # ---- 3. Instantiate FTS index ----
    print("[3/7] Initializing FullTextSearchCodebaseIndex ...")
    fts_index = FullTextSearchCodebaseIndex(db)
        # ---- 4. Instantiate LanceDB index ----
    print("[4/7] Initializing LanceDbIndex ...")
    lancedb_index = await LanceDbIndex.create(
        db=db,
        embeddings_provider=embeddings_provider,
        filesystem=fs
    )
    if lancedb_index is None:
        raise RuntimeError("Failed to create LanceDB instance")
    
    # ---- 5. Build the pipeline ----
    print("[5/7] Building NoRerankerRetrievalPipeline and RerankerRetrievalPipeline")
    
    no_rr = NoRerankerRetrievalPipeline(
        db=db,
        fts_index=fts_index,
        lance_index=lancedb_index,
        embeddings_provider=embeddings_provider,
        root_directory=root
    )
    
    print("[6/7] Initializing Reranker provider ...")
    reranker = CrossEncoderReranker(model_name="BAAI/bge-reranker-base")
    # reranker = CrossEncoderReranker(model_name="BAAI/bge-reranker-base")
    print(f"  Reranker model: {reranker.model_id}")

    rr = RerankerRetrievalPipeline(
        db=db,
        fts_index=fts_index,
        lance_index=lancedb_index,
        embeddings_provider=embeddings_provider,
        root_directory=root,
        reranker=reranker
    )
    
    # ---- 7. Run retrieval ----
    print(f"[7/7] Running {len(QUERIES)} queries ...\n")
    tags = get_current_tags(root)
    print(f"  Tags: {tags}\n")

    for label, query in QUERIES:
        print("=" * 78)
        print(f"[{label}] {query}")
        print("=" * 78)

        base_items = await no_rr.retrieve(query, tags=tags, top_k=TOP_K)
        rr_items = await rr.retrieve(query, tags=tags, top_k=TOP_K)

        base_rank = {it.chunk_id: i for i, it in enumerate(base_items)}
        rr_rank = {it.chunk_id: i for i, it in enumerate(rr_items)}

        print(f"\n  {'RANK':<5} {'Δ':<5} {'SCORE':<8} {'SOURCE':<28} PATH")
        print(f"  {'-'*5} {'-'*5} {'-'*8} {'-'*28} {'-'*40}")

        for i, it in enumerate(rr_items):
            prev = base_rank.get(it.chunk_id)
            if prev is None:
                delta = "NEW"
            elif prev == i:
                delta = "="
            elif prev > i:
                delta = f"+{prev - i}"     # moved up
            else:
                delta = f"-{i - prev}"     # moved down

            src = ",".join(sorted(it.source or []))[:27]
            path = it.path
            if len(path) > 60:
                path = "…" + path[-58:]
            print(f"  {i:<5} {delta:<5} {it.score:<8.4f} {src:<28} {path}")

        # What the reranker pulled in that RRF missed, and what it dropped
        new_ids = [it for it in rr_items if it.chunk_id not in base_rank]
        gone_ids = [it for it in base_items if it.chunk_id not in rr_rank]

        if new_ids:
            print("\n  ↑ Reranker surfaced (wasn't in RRF top-k):")
            for it in new_ids:
                print(f"      {it.score:.4f}  {it.symbol_name or it.path}")

        if gone_ids:
            print("\n  ↓ Reranker dropped (was in RRF top-k):")
            for it in gone_ids:
                print(f"      {it.score:.4f}  {it.symbol_name or it.path}")

        # If the correct answer for a query is known, mark it
        EXPECTED = {
            "res1": "ChunkCodebaseIndex",
            "res2": "BaseRetrievalPipeline",
            "res3": "insert_chunks",
            "res4": "FullTextSearchCodebaseIndex.update",
            "res5": "_fuse_weighted",
        }
        want = EXPECTED.get(label)
        if want:
            hit_base = next((i for i, it in enumerate(base_items)
                             if want in (it.symbol_name or "") or want in (it.path or "")), None)
            hit_rr = next((i for i, it in enumerate(rr_items)
                           if want in (it.symbol_name or "") or want in (it.path or "")), None)
            print(f"\n  ✓ target '{want}':  RRF=#{hit_base if hit_base is not None else 'miss'}  "
                  f"RR+rerank=#{hit_rr if hit_rr is not None else 'miss'}")

        print()

    print("=" * 78)
    print("Done.")
    # print("Run `python test_reranker.py --real` to use a real CrossEncoder.")
    print("=" * 78)
    

if __name__ == "__main__":
    asyncio.run(main())
    
    
        
    # -- Pick reranker -----------------------------------------------------
    # Start with MockReranker (no download) to verifyplumbing.
    # Swap to the CrossEncoder when you're ready for realscores.
    # USE_REAL = "--real" in sys.argv

    # if USE_REAL:
    #     print("    Reranker: CrossEncoderReranker(BAAIbge-reranker-base)")
    #     reranker = CrossEncoderReranker("BAAI/bge-reranker-base")
    # else:
    #     print("    Reranker: MockReranker (pass --real to usea real model)")
    #     reranker = MockReranker()
        

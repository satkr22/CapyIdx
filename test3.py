# tests/test_retrieval.py
"""
End-to-end test for NoRerankerRetrievalPipeline.

Run from your project root:
    python -m tests.test_retrieval
or:
    python tests/test_retrieval.py
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

# Add project root to sys.path so `retrieval`, `fts`, `lance_db` etc. resolve
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from base.db import SqliteDB
from fts.fullTextSearchCodebaseIndex import FullTextSearchCodebaseIndex
from lance_db.lanceDbIndex import LanceDbIndex
from retrieval.no_rerank_pipeline import NoRerankerRetrievalPipeline
from utils.parameters import RETRIEVAL_PARAMS
from retrieval.utils import get_current_tags
from embeddings.local import LocalEmbeddings
from utils.paths import get_index_sqlite_path
from utils.disk_operations import DiskOperations


# ---------------------------------------------------------------------------
# CONFIG — UPDATE THESE TO MATCH YOUR ENVIRONMENT
# ---------------------------------------------------------------------------

DB_PATH = get_index_sqlite_path()         
WORKSPACE_DIR = Path.cwd()                       
# QUERY = "Explain how chunkCodebaseIndex work?"            
QUERY = "how is retrieval working ??"            
# QUERY = "how are chunks stored in sqlite"            
# QUERY = "where does the FTS index get updated"          
# QUERY = "how does hybrid scoring work"          
TOP_K = 10


# ---------------------------------------------------------------------------
# Bootstrap: DB + Embeddings + Indexes + Pipeline
# ---------------------------------------------------------------------------

async def main():
    # ---- 1. Open SQLite connection ----
    print(f"[1/6] Opening SQLite DB at {DB_PATH} ...")
    if not DB_PATH.exists():
        print(f"  !! DB not found at {DB_PATH}. Update DB_PATH in the script.")
        return

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row 
    db.execute("PRAGMA foreign_keys = ON")

    # Sanity check: what tables exist?
    tables = [r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' OR type='view'"
    ).fetchall()]
    print(f"  Tables found: {sorted(tables)}")

    required_tables = {"chunks", "symbols", "fts_metadata", "chunk_tags", "lance_db_cache"}
    missing = required_tables - set(tables)
    if missing:
        print(f"  !! Missing required tables: {missing}")
        print("     Did you run the indexing pipeline first?")
        return

    # ---- 2. Instantiate embeddings provider ----
    print("[2/6] Initializing embeddings provider ...")
    
    
    # Adjust the constructor args to match YOUR Embeddings class.
    embeddings_provider = LocalEmbeddings("jinaai/jina-embeddings-v2-base-code")
    print(f"  Embedding model: {embeddings_provider.embedding_id}")
    
    # FileSystem
    fs = DiskOperations(roots=[str(WORKSPACE_DIR.resolve())])
    root = (await fs.get_workspace_dirs())
    # print("root------:", root)

    # ---- 3. Instantiate FTS index ----
    print("[3/6] Initializing FullTextSearchCodebaseIndex ...")
    fts_index = FullTextSearchCodebaseIndex(db)

    # ---- 4. Instantiate LanceDB index ----
    print("[4/6] Initializing LanceDbIndex ...")
    lancedb_index = await LanceDbIndex.create(
        db=db,
        embeddings_provider=embeddings_provider,
        filesystem=fs
    )
    if lancedb_index is None:
        raise RuntimeError("Failed to create LanceDB instance")

    # ---- 5. Build the pipeline ----
    print("[5/6] Building NoRerankerRetrievalPipeline ...")
    pipeline = NoRerankerRetrievalPipeline(
        db=db,
        fts_index=fts_index,
        lance_index=lancedb_index,
        embeddings_provider=embeddings_provider,
        root_directory=root
    )

    # ---- 6. Run retrieval ----
    print(f"[6/6] Running retrieval for: {QUERY!r}\n")
    tags = get_current_tags(root)
    print(f"  Tags: {tags}\n")

    # Inject a debug hook so we can see what happens at each stage
    await _run_with_debug(pipeline, QUERY, tags, TOP_K)


# ---------------------------------------------------------------------------
# Debug-enabled wrapper — prints stats at each pipeline stage
# ---------------------------------------------------------------------------

async def _run_with_debug(pipeline, query, tags, top_k):
    from retrieval.base_pipeline import extract_identifiers, anchor_score, _param

    print("=" * 70)
    print("STAGE 1+2: candidates (FTS + vector + symbol anchors)")
    print("=" * 70)
    print(f"  Identifiers in query: {extract_identifiers(query)}")
    print(f"  FTS query: {pipeline._build_fts_query(query)}")

    candidates = await pipeline._fetch_candidates(query, tags, n=RETRIEVAL_PARAMS["nRetrieve"])
    print(f"  Total unique candidates: {len(candidates)}")
    if not candidates:
        print("  !! No candidates retrieved. Aborting.")
        return

    def kind(c):
        s = set(c.sources)
        return "+".join(x for x in ("vector", "fts") if x in s) or "anchor-only"
    from collections import Counter
    print("  By source:", dict(Counter(kind(c) for c in candidates)))
    print("  Anchors:", sum(1 for c in candidates if anchor_score(c) > 0))

    print("\n  Top 5 by vector score (higher is better):")
    for c in sorted([c for c in candidates if c.vector_score is not None],
                    key=lambda x: -x.vector_score)[:5]:
        print(f"    {c.chunk_id[:8]}  sim={c.vector_score:.4f}  src={c.sources}")
    print("\n  Top 5 by bm25 (lower is better):")
    for c in sorted([c for c in candidates if c.bm25_score is not None],
                    key=lambda x: x.bm25_score)[:5]:
        print(f"    {c.chunk_id[:8]}  bm25={c.bm25_score:.4f}  src={c.sources}")

    print("\n" + "=" * 70)
    print("STAGE 2b: prune + fuse + path penalty")
    print("=" * 70)
    pruned = pipeline._prune_candidates(candidates)
    print(f"  Before prune: {len(candidates)}  After prune: {len(pruned)}")
    pipeline._fuse_rrf(pruned)
    pipeline._apply_path_penalty(pruned, query)
    pruned.sort(key=lambda c: c.final_score or 0.0, reverse=True)
    for c in pruned[:10]:
        print(f"    {c.chunk_id[:8]}  score={c.final_score:.4f}  src={c.sources}")

    print("\n" + "=" * 70)
    print("STAGE 3: symbol expansion (top anchors only)")
    print("=" * 70)
    top = pruned[:top_k]
    context = await pipeline._expand_via_symbols(
        top[: _param("anchorCount")], {c.chunk_id for c in top}
    )
    print(f"  Ranked: {len(top)}  Context chunks added: {len(context)}")

    print("\n" + "=" * 70)
    print("STAGES 4-7: full pipeline.retrieve()")
    print("=" * 70)
    for fusion in ("rrf", "weighted"):
        items = await pipeline.retrieve(query, tags, top_k=top_k, fusion=fusion)
        print(f"\n{'#' * 70}\n# {fusion.upper()} — {len(items)} ContextItems\n{'#' * 70}\n")
        for i, item in enumerate(items, 1):
            print(f"--- [{i}] score={item.score:.4f} ---")
            print(f"  path:    {item.path}:{item.start_line}-{item.end_line}")
            print(f"  symbol:  {item.symbol_type} {item.symbol_name}")
            print(f"  sources: {item.source}")
            snippet = item.content[:200].replace("\n", " ⏎ ")
            print(f"  content: {snippet}{'...' if len(item.content) > 200 else ''}\n")






if __name__ == "__main__":
    asyncio.run(main())
























'''
async def _run_with_debug(pipeline, query, tags, top_k):
    # --- Stage 1 & 2: candidate fetch + dedup ---
    print("=" * 70)
    print("STAGE 1+2: Fetching candidates (FTS + LanceDB → dedup)")
    print("=" * 70)
    candidates = await pipeline._fetch_candidates(
        query, tags, n=RETRIEVAL_PARAMS["nRetrieve"]
    )
    print(f"  Total unique candidates after dedup: {len(candidates)}")

    fts_only = sum(1 for c in candidates if c.sources == ["fts"])
    vec_only = sum(1 for c in candidates if c.sources == ["vector"])
    both     = sum(1 for c in candidates if c.sources and len(c.sources) == 2)
    print(f"  FTS-only:     {fts_only}")
    print(f"  Vector-only:  {vec_only}")
    print(f"  Both (overlap): {both}")

    # Print top 5 from each source
    print("\n  Top 5 by vector score (cosine similarity, higher is better):")
    for c in sorted([c for c in candidates if c.vector_score is not None],
                    key=lambda x: x.vector_score, reverse=True)[:5]:
        print(f"    {c.chunk_id[:8]}  sim={c.vector_score:.4f}  "
            f"src={c.sources}  path={c.chunk_data.filepath if c.chunk_data else '?'}")

    print("\n  Top 5 by bm25 score (lower is better):")
    for c in sorted([c for c in candidates if c.bm25_score is not None],key=lambda x: x.bm25_score)[:5]:
        print(f"    {c.chunk_id[:8]}  bm25={c.bm25_score:.4f}  "
            f"src={c.sources}  path={c.chunk_data.filepath if c.chunk_data else '?'}")

    if not candidates:
        print("\n  !! No candidates retrieved. Aborting.")
        return

    # --- Stage 3: symbol expansion ---
    print("\n" + "=" * 70)
    print("STAGE 3: Symbol expansion")
    print("=" * 70)
    expanded = await pipeline._expand_via_symbols(candidates)
    print(f"  Before expansion: {len(candidates)}")
    print(f"  After  expansion: {len(expanded)}")
    added = len(expanded) - len(candidates)
    print(f"  Sibling chunks added: {added}")
    if added:
        print("  Sample additions:")
        for c in expanded:
            if c.sources and "symbol_expansion" in c.sources:
                print(f"    {c.chunk_id[:8]}  src={c.sources}")
                if added <= 5:  # don't spam
                    continue

    # --- Stage 4: scoring (only if using NoReranker pipeline) ---
    print("\n" + "=" * 70)
    print("STAGE 4: Hybrid scoring (0.65*vec + 0.35*normBM25)")
    print("=" * 70)

    # Normalize BM25
    bm25_scores = [c.bm25_score for c in expanded if c.bm25_score is not None]
    if bm25_scores:
        min_bm25, max_bm25 = min(bm25_scores), max(bm25_scores)
        for c in expanded:
            if c.bm25_score is not None and max_bm25 > min_bm25:
                c.normalized_bm25 = (c.bm25_score - min_bm25) / (max_bm25 - min_bm25)
            elif c.bm25_score is not None:
                c.normalized_bm25 = 1.0
            else:
                c.normalized_bm25 = 0.0

    # Compute final score
    for c in expanded:
        vec = c.vector_score if c.vector_score is not None else 0.0
        bm25 = c.normalized_bm25 if c.normalized_bm25 is not None else 0.0
        c.final_score = (0.65 * vec) + (0.35 * bm25)

    expanded.sort(key=lambda x: x.final_score, reverse=True)
    top = expanded[:top_k]
    
    

    # --- Stage 5: materialize ---
    print("\n" + "=" * 70)
    print(f"STAGE 5: Materializing top {top_k} ContextItems")
    print("=" * 70)
    items = pipeline._materialize_context_items(top)

    # --- Print results ---
    print(f"\n\n{'#' * 70}")
    print(f"# FINAL RESULTS — {len(items)} ContextItems")
    print(f"{'#' * 70}\n")

    for i, item in enumerate(items, 1):
        print(f"--- [{i}] score={item.score:.4f} ---")
        print(f"  path:        {item.path}:{item.start_line}-{item.end_line}")
        print(f"  symbol:      {item.symbol_type} {item.symbol_name}  (id={item.symbol_id})")
        print(f"  sources:     {item.source}")
        snippet = item.content[:200].replace("\n", " ⏎ ")
        print(f"  content:     {snippet}{'...' if len(item.content) > 200 else ''}")
        print()

    # --- Summary stats ---
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Query:             {query}")
    print(f"  Candidates (nRetrieve={RETRIEVAL_PARAMS['nRetrieve']}): {len(candidates)}")
    print(f"  After expansion:   {len(expanded)}")
    print(f"  Returned (top_k={top_k}): {len(items)}")
    print(f"  By source:")
    src_counts = {}
    for it in items:
        for s in it.source:
            src_counts[s] = src_counts.get(s, 0) + 1
    for s, n in sorted(src_counts.items()):
        print(f"    {s:20s} {n}")
'''


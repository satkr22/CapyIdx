# retrieval/reranker_pipeline.py
from __future__ import annotations

from typing import List, Optional

from retrieval.base_pipeline import (
    BaseRetrievalPipeline,
    _param,
    _ph,
    _rparam,
    anchor_score,
)
from retrieval.models import Candidate, ContextItem
from retrieval.rerankers import BaseReranker
from base.index_d import BranchAndDir
from retrieval.utils import get_current_tags
from utils.parameters import RETRIEVAL_PARAMS, RERANK_DEFAULTS

# Tunables specific to reranking, same override pattern as base_pipeline's
# _DEFAULTS: add the key to RETRIEVAL_PARAMS to change it without touching code.


def _chunk_content(chunk_data) -> Optional[str]:
    """chunk_data may be a dict, a sqlite3.Row, or a plain object -- try
    mapping-style access first, then attribute access."""
    if chunk_data is None:
        return None
    try:
        return chunk_data["content"]
    except (KeyError, TypeError, IndexError):
        pass
    return getattr(chunk_data, "content", None)


class RerankerRetrievalPipeline(BaseRetrievalPipeline):
    """Same candidate generation / fusion / expansion as NoRerankerRetrievalPipeline,
    with one extra stage: the top slice of the fused ranking is re-scored by a
    pluggable reranker (a local cross-encoder or a hosted reranker API -- see
    retrieval/rerankers.py) before the final top_k cut.

    Wire in any BaseReranker implementation:

        from retrieval.rerankers import CrossEncoderReranker, CohereReranker

        pipeline = RerankerRetrievalPipeline(
            db, fts_index, lance_index, embeddings_provider, root_directory,
            reranker=CrossEncoderReranker("BAAI/bge-reranker-base"),   # local
            # reranker=CohereReranker(api_key="..."),                  # or hosted API
        )

    `reranker=None` makes this behave exactly like NoRerankerRetrievalPipeline
    (the rerank stage is a no-op), so this class can be the only pipeline you
    keep around -- swap the reranker in config rather than swapping classes.
    """

    async def retrieve(
        self,
        query: str,
        tags: list[BranchAndDir] | None = None,
        top_k: int = RETRIEVAL_PARAMS["nFinal"],
        recent_files: list[str] | None = None,
        fusion: str = "rrf",  # "rrf" (default) or "weighted"
        rerank_pool_size: Optional[int] = None,
    ) -> list[ContextItem]:

        if not tags:
            tags = get_current_tags(self.root_directory)

        # 1. Candidates: FTS + vector + symbol-name anchors, deduplicated
        candidates = await self._fetch_candidates(
            query, tags, n=RETRIEVAL_PARAMS["nRetrieve"]
        )
        if not candidates:
            return []

        # 2. Drop far-below-best vector-only neighbours
        candidates = self._prune_candidates(candidates)

        # 3. Fuse first-stage scores. This decides which candidates are even
        #    worth sending to the (slower, costlier) reranker.
        if fusion == "weighted":
            self._fuse_weighted(candidates)
        else:
            self._fuse_rrf(candidates)

        # 4. Down-weight tests/docs/readme unless the query asks for them
        self._apply_path_penalty(candidates, query)

        candidates.sort(key=lambda c: c.final_score or 0.0, reverse=True)

        # 5. Rerank: only the top slice goes to the reranker; everything
        #    below the pool keeps its fused score untouched.
        pool_size = rerank_pool_size or _rparam("rerankPoolSize")
        if self.reranker is not None and candidates:
            await self._rerank(candidates[:pool_size], query)
            candidates.sort(key=lambda c: c.final_score or 0.0, reverse=True)

        top = candidates[:top_k]

        # 6. Symbol expansion AFTER ranking, around the top anchors only.
        #    Returns only new chunks; originals keep their own scores.
        known = {c.chunk_id for c in top}
        expansion_anchors = [
            c
            for c in top
            if anchor_score(c) > 0 or "fts" in (c.sources or [])
        ]
        context = await self._expand_via_symbols(
            expansion_anchors[: _param("anchorCount")], known
        )

        selected = top + context

        # 7. Recently edited files: add a few chunks at a modest score
        if recent_files:
            ids = {c.chunk_id for c in selected}
            floor = 0.5 * (top[0].final_score or 0.0)
            rows = self.db.execute(
                f"SELECT id FROM chunks WHERE path IN ({_ph(recent_files)}) ORDER BY path, idx LIMIT ?",
                (*recent_files, max(1, top_k // 4)),
            ).fetchall()
            for r in rows:
                if r["id"] not in ids:
                    rc = Candidate(chunk_id=r["id"], sources=["recent_file"])
                    rc.final_score = floor
                    selected.append(rc)

        # 8. Materialise (grouped per symbol), collapse nested ranges, cap at top_k
        items = self._materialize_context_items(selected)
        items = self._drop_nested(items)
        items.sort(key=lambda i: i.score, reverse=True)
        return items[:top_k]

    # ------------------------------------------------------------------
    # Reranking
    # ------------------------------------------------------------------
    async def _rerank(self, pool: List[Candidate], query: str) -> None:
        """Mutates each Candidate in `pool` in place. Fetches passage text for
        anything that doesn't already carry it, scores the batch with
        self.reranker, min-max normalizes those scores onto the fused score's
        own range (so downstream logic that assumes a consistent final_score
        scale -- e.g. the recent-files `floor` in retrieve() -- still behaves
        sanely), and blends them in by self.rerank_weight (1.0 = reranker
        ranking wins outright within the pool)."""
        if not pool:
            return

        reranker = self.reranker
        if reranker is None:
            return

        texts = self._rerank_texts(pool)

        try:
            raw_scores = await reranker.score(query, texts)
        except Exception:
            # Reranker unreachable/erroring -> fall back to the fused ranking
            # for this pool rather than dropping context entirely.
            return

        if len(raw_scores) != len(pool):
            return

        lo, hi = min(raw_scores), max(raw_scores)
        spread = hi - lo

        fused = [c.final_score or 0.0 for c in pool]
        f_lo, f_hi = min(fused), max(fused)
        f_spread = f_hi - f_lo or 1.0

        w = self.rerank_weight
        for c, raw, base in zip(pool, raw_scores, fused):
            norm = (raw - lo) / spread if spread > 0 else 0.5
            rescaled = f_lo + norm * f_spread  # onto the fused score's own range
            c.final_score = (1 - w) * base + w * rescaled

    def _rerank_texts(self, pool: List[Candidate]) -> List[str]:
        """Passage text per candidate, in pool order. Reuses chunk_data's
        content when a candidate already carries it (vector/fts hits do);
        pulls the rest (symbol anchors, etc.) from the DB in one query."""
        texts: dict[str, str] = {}
        missing = []
        for c in pool:
            content = _chunk_content(c.chunk_data)
            if content:
                texts[c.chunk_id] = content
            else:
                missing.append(c.chunk_id)

        if missing:
            rows = self.db.execute(
                f"SELECT id, content FROM chunks WHERE id IN ({_ph(missing)})", missing
            ).fetchall()
            for r in rows:
                texts[r["id"]] = r["content"]

        max_chars = _rparam("rerankMaxChars")
        return [(texts.get(c.chunk_id) or "")[:max_chars] for c in pool]

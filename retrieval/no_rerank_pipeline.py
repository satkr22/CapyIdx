# retrieval/no_rerank_pipeline.py
from retrieval.base_pipeline import BaseRetrievalPipeline
from retrieval.models import Candidate, ContextItem
from base.index_d import BranchAndDir
from retrieval.utils import get_current_tags
from utils.parameters import RETRIEVAL_PARAMS

from retrieval.base_pipeline import _param, _ph, anchor_score


class NoRerankerRetrievalPipeline(BaseRetrievalPipeline):

    async def retrieve(
        self,
        query: str,
        tags: list[BranchAndDir] | None = None,
        top_k: int = RETRIEVAL_PARAMS["nFinal"],
        recent_files: list[str] | None = None,
        fusion: str = "rrf",  # "rrf" (default) or "weighted" (0.65*vec + 0.35*bm25, direction fixed)
    ) -> list[ContextItem]:

        if not tags:
            tags = get_current_tags(self.root_directory)  # was super().root_directory -> AttributeError

        # 1. Candidates: FTS + vector + symbol-name anchors, deduplicated
        candidates = await self._fetch_candidates(
            query, tags, n=RETRIEVAL_PARAMS["nRetrieve"]
        )
        if not candidates:
            return []

        # 2. Drop far-below-best vector-only neighbours
        candidates = self._prune_candidates(candidates)


        # 3. Fuse scores (real scores only; nothing inherited yet)
        if fusion == "weighted":
            self._fuse_weighted(candidates)
        else:
            self._fuse_rrf(candidates)

        # 4. Down-weight tests/docs/readme unless the query asks for them
        self._apply_path_penalty(candidates, query)

        candidates.sort(key=lambda c: c.final_score or 0.0, reverse=True)
        top = candidates[:top_k]

        # 5. Symbol expansion AFTER ranking, around the top anchors only.
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

        # 6. Recently edited files: add a few chunks at a modest score
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

        # 7. Materialise (grouped per symbol), collapse nested ranges, cap at top_k
        items = self._materialize_context_items(selected)
        items = self._drop_nested(items)
        items.sort(key=lambda i: i.score, reverse=True)
        return items[:top_k]

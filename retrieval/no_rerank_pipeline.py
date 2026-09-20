# retrieval/no_rerank_pipeline.py
from retrieval.base_pipeline import BaseRetrievalPipeline
from retrieval.models import Candidate, ContextItem
from base.index_d import BranchAndDir
from retrieval.utils import get_current_tags
from utils.parameters import RETRIEVAL_PARAMS

class NoRerankerRetrievalPipeline(BaseRetrievalPipeline):
    
    async def retrieve(
        self, 
        query: str, 
        tags: list[BranchAndDir] | None = None, 
        top_k: int = RETRIEVAL_PARAMS["nFinal"], 
        recent_files: list[str] | None = None
    ) -> list[ContextItem]:
        
        
        if not tags:
            tags = get_current_tags(super().root_directory)
 
        # 1. Fetch Candidates (FTS + LanceDB -> deduplicated)
        candidates = await self._fetch_candidates(query, tags, n=30)
        
        if not candidates:
            return []
            
        # 2. Expand via Symbols (The differentiator)
        expanded_candidates = await self._expand_via_symbols(candidates)
        
        # 3. Normalize BM25 scores (min-max normalization)
        bm25_scores = [c.bm25_score for c in expanded_candidates if c.bm25_score is not None]
        if bm25_scores:
            min_bm25 = min(bm25_scores)
            max_bm25 = max(bm25_scores)
            for c in expanded_candidates:
                if c.bm25_score is not None and max_bm25 > min_bm25:
                    c.normalized_bm25 = (c.bm25_score - min_bm25) / (max_bm25 - min_bm25)
                elif c.bm25_score is not None:
                    c.normalized_bm25 = 1.0 
                else:
                    c.normalized_bm25 = 0.0
        else:
            for c in expanded_candidates:
                c.normalized_bm25 = 0.0

        # 4. Calculate Hybrid Score (PDF's recommendation: 0.65 * cosine + 0.35 * normalized_bm25)
        for c in expanded_candidates:
            # IMPORTANT: Ensure your LanceDB returns a similarity (higher is better), 
            # not a distance (lower is better). If it returns distance, convert it here.
            # e.g., vector_score = 1.0 / (1.0 + c.vector_score)
            vector_score = c.vector_score if c.vector_score is not None else 0.0
            normalized_bm25 = c.normalized_bm25 if c.normalized_bm25 is not None else 0.0
            c.final_score = (0.65 * vector_score) + (0.35 * normalized_bm25)

        # 5. Sort by final_score descending and take top_k
        expanded_candidates.sort(
            key=lambda x: x.final_score if x.final_score is not None else 0.0,
            reverse=True,
        )
        top_candidates = expanded_candidates[:top_k]
        
        
        if recent_files:
            # Query chunks table for these paths and add them as high-priority candidates
            placeholders = ",".join("?" for _ in recent_files)
            recent_chunks = self.db.execute(
                f"SELECT id FROM chunks WHERE path IN ({placeholders}) LIMIT ?",
                (*recent_files, top_k // 4) # Give them ~25% of top_k slots
            ).fetchall()
            
            # Add them to the top of your final sorted list
            for rc in recent_chunks:
                # (Create ContextItem and inject into the list)
                pass
        
        # 6. Materialize and return ContextItems
        return self._materialize_context_items(top_candidates)
    
    
    
    
    '''
    # If want to add embedding expansion (Optional) add this is rerankerRetrievalPipeline:
    async def _expand_with_embeddings(self, top_candidates: list[Candidate]) -> list[Candidate]:
        """Stage 3.5: Pseudo-relevance feedback via embeddings."""
        if not top_candidates:
            return top_candidates
        
        expanded = list(top_candidates)
        top_for_expansion = top_candidates[:RETRIEVAL_PARAMS["nResultsToExpandWithEmbeddings"]]
        
        for cand in top_for_expansion:
            # 1. Fetch the chunk content
            row = self.db.execute("SELECT content FROM chunks WHERE id = ?", (cand.chunk_id,)).fetchone()
            if not row:
                continue
            # 2. Embed it
            embedding = (await self.embeddings_provider.embed([row["content"]]))[0]
            # 3. Search LanceDB for similar chunks
            similar = await self.lance_index.search_by_vector(
                embedding, 
                n=RETRIEVAL_PARAMS["nEmbeddingsExpandTo"],
                tags=self.current_tags,
            )
            # 4. Add them as new candidates with lower weight
            for chunk, score, chunk_id in similar:
                expanded.append(Candidate(
                    chunk_id=chunk_id,
                    vector_score=score,
                    sources=["embedding_expansion"],
                ))
        return expanded
        
    
    '''
    
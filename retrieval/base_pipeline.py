# retrieval/base_pipeline.py
from abc import ABC, abstractmethod
from typing import List, Optional
import sqlite3
import subprocess
import os
import re

from base.index_d import Chunk, RetrieveConfig, BranchAndDir
from fts.fullTextSearchCodebaseIndex import FullTextSearchCodebaseIndex
from lance_db.lanceDbIndex import LanceDbIndex
from retrieval.models import Candidate, ContextItem
from embeddings.base import Embeddings
from retrieval.utils import get_current_tags
from utils.parameters import RETRIEVAL_PARAMS
from utils.paths import get_lance_db_path

class BaseRetrievalPipeline(ABC):
    def __init__(
        self, 
        db: sqlite3.Connection, 
        fts_index: FullTextSearchCodebaseIndex,
        lance_index: LanceDbIndex,
        embeddings_provider: Embeddings,
        root_directory: List[str]
    ):
        self.db = db
        self.fts_index = fts_index
        self.lance_index = lance_index
        self.embeddings_provider = embeddings_provider
        self.root_directory = root_directory

    async def _fetch_candidates(
        self, 
        query: str, 
        tags: List[BranchAndDir], # MUST BE PASSED IN
        n: int = RETRIEVAL_PARAMS["nRetrieve"],
    ) -> List[Candidate]:
        
        # 1. Fetch from FTS
        # The FTS index needs a RetrieveConfig object containing the tags
        cleaned_fts_query = self._get_cleaned_trigrams(query)
        
        if not tags:
            tags = self._build_default_tags()
            
        fts_config = RetrieveConfig(
            text=cleaned_fts_query, 
            n=n, 
            tags=tags,
            filter_paths=None, # Optional: restrict to specific files
            bm25_threshold=RETRIEVAL_PARAMS["bm25Threshold"] # Uses default from RETRIEVAL_PARAMS
        )
        fts_results = await self.fts_index.retrieve(fts_config)
        
        # 2. Fetch from LanceDB
        # LanceDB retrieve() takes tags directly as a list
        query_embeddings = await self.embeddings_provider.embed([query])
        
        lance_results = await self.lance_index.retrieve(
            query=query, 
            n=n, 
            tags=tags,
            filter_directory=None
        )

        # 3. Merge and Deduplicate by chunk_id
        # (Assuming you modified the retrieve() methods to return tuples: (Chunk, score, chunk_id))
        merged: dict[str, Candidate] = {}
        
        # Process LanceDB results (Vector scores)
        for chunk, score, chunk_id in lance_results:
            merged[chunk_id] = Candidate(
                chunk_id=chunk_id,
                vector_score=score,
                sources=["vector"],
                chunk_data=chunk 
            )
            
        # Process FTS results (BM25 scores)
        for chunk, score, chunk_id in fts_results:
            if chunk_id in merged:
                merged[chunk_id].bm25_score = score
                merged[chunk_id].sources.append("fts") # type: ignore
            else:
                merged[chunk_id] = Candidate(
                    chunk_id=chunk_id,
                    bm25_score=score,
                    sources=["fts"],
                    chunk_data=chunk
                )
                
        return list(merged.values())
    
    def _build_default_tags(self) -> list[BranchAndDir]:
        """Builds a fallback tag if the user doesn't provide one."""
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], 
                text=True
            ).strip()
        except Exception:
            branch = "main" # Fallback if not a git repo

        # Use current working directory as the default directory
        current_dir = os.path.abspath(os.getcwd())
        
        return [BranchAndDir(branch=branch, directory=current_dir)]
    
       
    async def _expand_via_symbols(self, candidates: List[Candidate]) -> List[Candidate]:
        """Stage 3: Symbol Expansion. Fetch sibling chunks for coherent context."""
        if not candidates:
            return []

        expanded_candidates: List[Candidate] = []
        seen_chunks = set()
        
        # 1. Batch fetch symbolIds for all candidate chunk_ids to avoid N+1 queries
        chunk_ids = [c.chunk_id for c in candidates]
        placeholders = ",".join("?" for _ in chunk_ids)
        
        chunk_rows = self.db.execute(
            f"SELECT id, symbolId FROM chunks WHERE id IN ({placeholders})",
            chunk_ids
        ).fetchall()
        
        chunk_to_symbol = {row["id"]: row["symbolId"] for row in chunk_rows}
        
        # 2. Batch fetch parentIds for all unique symbolIds
        symbol_ids = list(set(sid for sid in chunk_to_symbol.values() if sid))
        symbol_to_parent = {}
        
        if symbol_ids:
            sym_placeholders = ",".join("?" for _ in symbol_ids)
            symbol_rows = self.db.execute(
                f"SELECT id, parentId FROM symbols WHERE id IN ({sym_placeholders})",
                symbol_ids
            ).fetchall()
            symbol_to_parent = {row["id"]: row["parentId"] for row in symbol_rows}
            
        # 3. Fetch sibling chunks for each candidate
        for cand in candidates:
            if cand.chunk_id in seen_chunks:
                continue
            
            seen_chunks.add(cand.chunk_id)
            
            symbol_id = chunk_to_symbol.get(cand.chunk_id)
            if not symbol_id:
                # Not a symbol chunk (e.g., a generic block), just keep it as is
                expanded_candidates.append(cand)
                continue
            
            # Original candidate (with symbol) is preserved as-is
            expanded_candidates.append(cand)
                
            parent_id = symbol_to_parent.get(symbol_id)
            
            # Fetch all chunks for this symbol and its parent (if it exists)
            if parent_id:
                sibling_rows = self.db.execute(
                    "SELECT id FROM chunks WHERE symbolId = ? OR symbolId = ?",
                    (symbol_id, parent_id)
                ).fetchall()
            else:
                sibling_rows = self.db.execute(
                    "SELECT id FROM chunks WHERE symbolId = ?",
                    (symbol_id,)
                ).fetchall()
                
            for sib in sibling_rows:
                sib_id = sib["id"]
                if sib_id not in seen_chunks:
                    seen_chunks.add(sib_id)
                    # Create a new Candidate inheriting the original scores/sources
                    expanded_candidates.append(Candidate(
                        chunk_id=sib_id,
                        vector_score=cand.vector_score,
                        bm25_score=cand.bm25_score,
                        sources=(cand.sources or []) + ["symbol_expansion"]
                    ))
                    
        return expanded_candidates


    def _materialize_context_items(self, candidates: List[Candidate]) -> List[ContextItem]:
        if not candidates:
            return []
        
        chunk_ids = [c.chunk_id for c in candidates]
        placeholders = ",".join("?" for _ in chunk_ids)
        
        rows = self.db.execute(f"""
            SELECT 
                c.id as chunk_id, c.path, c.startLine, c.endLine, c.content,
                c.symbolId, c.pieceIndex, c.pieceCount, c.idx,
                s.id as symbol_id, s.name as symbol_name, s.type as symbol_type
            FROM chunks c
            LEFT JOIN symbols s ON c.symbolId = s.id
            WHERE c.id IN ({placeholders})
        """, chunk_ids).fetchall()
        
        row_map = {r["chunk_id"]: r for r in rows}
        
        # Group by symbol_id (or by chunk_id if no symbol)
        groups: dict[str, list[Candidate]] = {}
        for cand in candidates:
            row = row_map.get(cand.chunk_id)
            if not row:
                continue
            key = row["symbol_id"] or cand.chunk_id
            groups.setdefault(key, []).append(cand)
        
        context_items = []
        for sym_key, group in groups.items():
            # Sort group by pieceIndex to reconstruct in order
            group.sort(key=lambda c: row_map[c.chunk_id]["pieceIndex"])
            
            # Use the highest-scoring candidate as the "representative"
            best = max(group, key=lambda c: c.final_score or 0.0)
            row = row_map[best.chunk_id]
            
            # Merge pieces of the same symbol
            if len(group) > 1:
                merged_content = "\n\n".join(row_map[c.chunk_id]["content"] for c in group)
                start_line = min(row_map[c.chunk_id]["startLine"] for c in group)
                end_line = max(row_map[c.chunk_id]["endLine"] for c in group)
            else:
                merged_content = row["content"]
                start_line = row["startLine"]
                end_line = row["endLine"]
            
            # Combine source provenance across all pieces
            all_sources = set()
            for c in group:
                for s in (c.sources or []):
                    all_sources.add(s)
            
            context_items.append(ContextItem(
                chunk_id=best.chunk_id,
                symbol_id=row["symbol_id"],
                symbol_name=row["symbol_name"],
                symbol_type=row["symbol_type"],
                path=row["path"],
                start_line=start_line,
                end_line=end_line,
                content=merged_content,
                score=best.final_score or 0.0,
                source=list(all_sources),
            ))
        
        return context_items
    

    
    def _get_cleaned_trigrams(self, query: str) -> str:
        """
        Build FTS5 query: OR of per-word AND-groups.
        Each word's trigrams must ALL appear (AND), but only ONE word needs to match (OR).
        """

        text = " ".join(query.split()).lower()
        words = re.findall(r'\b\w+\b', text)
        
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at",
            "to", "for", "of", "with", "by", "is", "are", "was",
            "were", "how", "does", "do", "what", "why", "when",
            "where", "which", "it", "this", "that", "i", "you",
            "explain", "describe", "show", "tell", "me",
        }
        words = [w for w in words if w not in stop_words and len(w) >= 3]
        
        if not words:
            return ""
        
        groups = []
        for word in words:
            # Trigrams of this single word
            word_trigrams = []
            for i in range(len(word) - 2):
                tg = word[i:i+3].replace('"', '""')
                word_trigrams.append(f'"{tg}"')
            
            if not word_trigrams:
                continue
            
            # All trigrams of ONE word must appear together (AND)
            groups.append("(" + " AND ".join(word_trigrams) + ")")
        
        if not groups:
            return ""
        
        # Any group matching is enough (OR)
        return " OR ".join(groups)
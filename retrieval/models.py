# retrieval/models.py
from dataclasses import dataclass
from typing import Optional, List

from base.index_d import Chunk

@dataclass
class Candidate:
    """Used during the retrieval phase before full content is fetched."""
    chunk_id: str
    chunk_data: Optional[Chunk] = None
    vector_score: Optional[float] = None
    bm25_score: Optional[float] = None
    normalized_bm25: Optional[float] = None 
    final_score: Optional[float] = None     
    sources: Optional[List[str]] = None  # ["vector"], ["fts"], ["vector", "fts"], ["symbol_expansion"]

@dataclass
class ContextItem:
    """The final enriched output sent to the LLM."""
    chunk_id: str
    symbol_id: Optional[str]
    symbol_name: Optional[str]
    symbol_type: Optional[str]
    path: str
    start_line: int
    end_line: int
    content: str
    score: float  # Final reranker score or hybrid score
    source: List[str]  # Provenance of this context
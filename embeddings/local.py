from __future__ import annotations

import asyncio
from typing import List

from sentence_transformers import SentenceTransformer


# Sensible defaults per model family. max_embedding_chunk_size is in tokens
# (matches what chunker.basic / chunker.chunk expect).
_MODEL_DEFAULTS = {
    "sentence-transformers/all-MiniLM-L6-v2": 256,
    "sentence-transformers/all-mpnet-base-v2": 384,
    "BAAI/bge-small-en-v1.5": 512,
    "BAAI/bge-base-en-v1.5": 512,
    "nomic-ai/nomic-embed-text-v1.5": 512,
    "jinaai/jina-embeddings-v2-base-code": 512,
}

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_DEFAULT_CHUNK_SIZE = 512


class LocalEmbeddings:
    """Concrete Embeddings provider backed by sentence-transformers.

    Satisfies the `Embeddings` Protocol structurally — no inheritance required.
    """

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL,
        max_embedding_chunk_size: int | None = None,
        device: str | None = None,
        batch_size: int = 32,
    ) -> None:
        self._model = SentenceTransformer(model_name, device=device)
        self._batch_size = batch_size
        self.embedding_id = f"sentence-transformers::{model_name}"
        self.max_embedding_chunk_size = (
            max_embedding_chunk_size
            if max_embedding_chunk_size is not None
            else _MODEL_DEFAULTS.get(model_name, _DEFAULT_CHUNK_SIZE)
        )

    async def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        # encode() is CPU/GPU bound and releases the GIL in its C/CUDA path,
        # so offloading to a thread keeps the event loop free.
        vectors = await asyncio.to_thread(
            self._model.encode,
            texts,
            batch_size=self._batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.tolist()
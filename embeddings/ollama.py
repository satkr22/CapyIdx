from __future__ import annotations

from typing import List

import ollama  # pip install ollama


class OllamaEmbeddings:
    def __init__(
        self,
        model: str = "nomic-embed-text",
        host: str | None = None,
        max_embedding_chunk_size: int = 512,
    ) -> None:
        self._model = model
        self._client = ollama.AsyncClient(host=host) if host else ollama.AsyncClient()
        self.embedding_id = f"ollama::{model}"
        self.max_embedding_chunk_size = max_embedding_chunk_size

    async def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        response = await self._client.embed(model=self._model, input=texts)
        return response["embeddings"]
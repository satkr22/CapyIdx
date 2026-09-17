from __future__ import annotations

from typing import List

from openai import AsyncOpenAI  # pip install openai

from embeddings.base import Embeddings

# text-embedding-3-* accept 8191 tokens, but for code chunks 512–1024 is
# plenty and keeps cost/latency down.
_MODEL_DEFAULTS = {
    "text-embedding-3-small": 512,
    "text-embedding-3-large": 512,
    "text-embedding-ada-002": 512,
}


class OpenAIEmbeddings(Embeddings):
    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        max_embedding_chunk_size: int | None = None,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key)  # falls back to env var
        self._model = model
        self.embedding_id = f"openai::{model}"
        self.max_embedding_chunk_size = (
            max_embedding_chunk_size
            if max_embedding_chunk_size is not None
            else _MODEL_DEFAULTS.get(model, 512)
        )

    async def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        resp = await self._client.embeddings.create(
            model=self._model,
            input=texts,
        )
        # API preserves input order in `resp.data`.
        return [d.embedding for d in resp.data]
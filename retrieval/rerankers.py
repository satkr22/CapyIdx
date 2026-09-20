# retrieval/rerankers.py
"""
Pluggable reranker interfaces for the retrieval pipeline.

Implementations:
  - BaseReranker          abstract interface: `async def score(query, passages) -> List[float]`
  - CrossEncoderReranker  local model via sentence-transformers (ms-marco-MiniLM, BGE, ...)
  - APIReranker           generic HTTP client for hosted rerankers (Cohere, Jina, Voyage, ...)
  - CohereReranker / JinaReranker / VoyageReranker  thin convenience subclasses of APIReranker

All implementations return one float per passage, in the same order as the
input list -- higher means more relevant. No assumption is made about scale
(raw cross-encoder logits, 0-1 probabilities, whatever an API returns); the
pipeline that calls `score()` normalizes the results itself, so you can swap
one reranker for another without touching the pipeline.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import List, Optional


class BaseReranker(ABC):
    """Implement `score()` to plug in any reranker -- local model or hosted API."""

    @abstractmethod
    async def score(self, query: str, passages: List[str]) -> List[float]:
        """One relevance score per passage, same order as `passages`.
        Higher = more relevant."""
        raise NotImplementedError


class CrossEncoderReranker(BaseReranker):
    """Local reranker via sentence-transformers' CrossEncoder.

    Good defaults:
      - "cross-encoder/ms-marco-MiniLM-L-6-v2"  fast, English, ~80MB, CPU-friendly
      - "BAAI/bge-reranker-base"                 stronger, multilingual
      - "BAAI/bge-reranker-v2-m3"                strongest, slower/bigger, multilingual
      
    Requires: pip install sentence-transformers
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        device: Optional[str] = None,
        batch_size: int = 32,
        max_length: int = 512,
    ):
        from sentence_transformers import CrossEncoder  # lazy import, optional dep
        self.model_id = f"sentence-transformers::{model_name}"
        self._model = CrossEncoder(model_name, device=device, max_length=max_length, trust_remote_code=True)
        self._batch_size = batch_size

    async def score(self, query: str, passages: List[str]) -> List[float]:
        if not passages:
            return []
        pairs = [[query, p] for p in passages]
        loop = asyncio.get_event_loop()
        # CrossEncoder.predict() is blocking (CPU/GPU-bound) -- run it off
        # the event loop so it doesn't stall the rest of the pipeline.
        scores = await loop.run_in_executor(
            None, lambda: self._model.predict(pairs, batch_size=self._batch_size)
        )
        return [float(s) for s in scores]


class APIReranker(BaseReranker):
    """Generic HTTP client for hosted reranker APIs that follow the
    `{"query": ..., "documents": [...]}` -> `{"results": [{"index", "relevance_score"}]}`
    shape. This covers Cohere's /v1/rerank and Jina's /v1/rerank as-is, and
    Voyage's /v1/rerank too (its response key is "data" instead of "results",
    handled in `_parse_response`). For a provider with a different request/
    response shape, subclass this and override `_build_payload` and/or
    `_parse_response`.

    Requires: pip install httpx
    """

    def __init__(
        self,
        api_url: str,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 20.0,
        max_batch: int = 100,
        extra_headers: Optional[dict] = None,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_batch = max_batch  # docs per request; large pools are split and run concurrently
        self.extra_headers = extra_headers or {}
        self._client = None  # lazily created httpx.AsyncClient, reused across calls

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_payload(self, query: str, passages: List[str]) -> dict:
        payload = {"query": query, "documents": passages}
        if self.model:
            payload["model"] = self.model
        return payload

    def _parse_response(self, data: dict, n: int) -> List[float]:
        scores = [0.0] * n
        for r in data.get("results") or data.get("data") or []:
            idx = r.get("index")
            val = r.get("relevance_score", r.get("score"))
            if idx is not None and val is not None and 0 <= idx < n:
                scores[idx] = float(val)
        return scores

    async def _get_client(self):
        import httpx  # lazy import, optional dep

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _score_batch(self, query: str, passages: List[str]) -> List[float]:
        client = await self._get_client()
        resp = await client.post(
            self.api_url, headers=self._headers(), json=self._build_payload(query, passages)
        )
        resp.raise_for_status()
        return self._parse_response(resp.json(), len(passages))

    async def score(self, query: str, passages: List[str]) -> List[float]:
        if not passages:
            return []
        if len(passages) <= self.max_batch:
            return await self._score_batch(query, passages)

        batches = [
            passages[i : i + self.max_batch] for i in range(0, len(passages), self.max_batch)
        ]
        results = await asyncio.gather(*(self._score_batch(query, b) for b in batches))
        out: List[float] = []
        for r in results:
            out.extend(r)
        return out

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --- Convenience subclasses for common providers ---------------------------
# Each just pins the URL/default model; all still take api_key + any APIReranker kwarg.

class CohereReranker(APIReranker):
    """https://docs.cohere.com/reference/rerank"""

    def __init__(self, api_key: str, model: str = "rerank-v3.5", **kwargs):
        super().__init__(
            api_url="https://api.cohere.com/v1/rerank", api_key=api_key, model=model, **kwargs
        )


class JinaReranker(APIReranker):
    """https://jina.ai/reranker/"""

    def __init__(self, api_key: str, model: str = "jina-reranker-v2-base-multilingual", **kwargs):
        super().__init__(
            api_url="https://api.jina.ai/v1/rerank", api_key=api_key, model=model, **kwargs
        )


class VoyageReranker(APIReranker):
    """https://docs.voyageai.com/docs/reranker"""

    def __init__(self, api_key: str, model: str = "rerank-2", **kwargs):
        super().__init__(
            api_url="https://api.voyageai.com/v1/rerank", api_key=api_key, model=model, **kwargs
        )
from typing import Protocol, List

class Embeddings(Protocol):
    embedding_id: str
    max_embedding_chunk_size: int

    async def embed(self, texts: List[str]) -> List[List[float]]:
        ...
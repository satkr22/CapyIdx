# retrieval/retrieval.py
from .rerank_pipeline import RerankerRetrievalPipeline
from .no_rerank_pipeline import NoRerankerRetrievalPipeline

async def retrieve(query: str, db_conn, lance_db, reranker_client=None, top_k=15, include_recent=False):
    if reranker_client:
        pipeline = RerankerRetrievalPipeline(db_conn, lance_db, reranker_client)
    else:
        pipeline = NoRerankerRetrievalPipeline(db_conn, lance_db)
        
    return await pipeline.retrieve(query, top_k, include_recent)
# retrieval/retrieval.py
from retrieval.rerank_pipeline import RerankerRetrievalPipeline
from retrieval.no_rerank_pipeline import NoRerankerRetrievalPipeline

async def retrieve(query: str, db_conn, lance_db, reranker_client=None, top_k=15, include_recent=False):
    if reranker_client:
        pipeline = RerankerRetrievalPipeline(db_conn, lance_db, reranker_client)
    else:
        pipeline = NoRerankerRetrievalPipeline(db_conn, lance_db)
        
    return await pipeline.retrieve(query, top_k, include_recent)




# # local
# pipeline = RerankerRetrievalPipeline(
#     db, fts_index, lance_index, embeddings_provider, root_directory,
#     reranker=CrossEncoderReranker("BAAI/bge-reranker-base"),
# )

# # or hosted API
# pipeline = RerankerRetrievalPipeline(
#     db, fts_index, lance_index, embeddings_provider, root_directory,
#     reranker=CohereReranker(api_key="..."),
# )

# items = await pipeline.retrieve(query, top_k=10)
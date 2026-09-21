from retrieval.rerank_pipeline import RerankerRetrievalPipeline
from retrieval.no_rerank_pipeline import NoRerankerRetrievalPipeline

async def retrieve(
    query: str,
    db_conn,
    fts_index,
    lance_db,
    embeddings_provider,
    root_directory,
    reranker_client=None,
    top_k=15,
    tags=None,
    recent_files=None,
    fusion="rrf",
):
    if reranker_client:
        pipeline = RerankerRetrievalPipeline(
            db=db_conn,
            fts_index=fts_index,
            lance_index=lance_db,
            embeddings_provider=embeddings_provider,
            root_directory=root_directory,
            reranker=reranker_client,
        )
    else:
        pipeline = NoRerankerRetrievalPipeline(
            db=db_conn,
            fts_index=fts_index,
            lance_index=lance_db,
            embeddings_provider=embeddings_provider,
            root_directory=root_directory,
        )

    return await pipeline.retrieve(
        query,
        tags=tags,
        top_k=top_k,
        recent_files=recent_files,
        fusion=fusion,
    )
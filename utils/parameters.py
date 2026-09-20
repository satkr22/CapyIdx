RETRIEVAL_PARAMS = {
    "rerankThreshold": 0.3,
    "nFinal": 20,
    "nRetrieve": 50,
    "bm25Threshold": -2.5,
    "nResultsToExpandWithEmbeddings": 5,
    "nEmbeddingsExpandTo": 5,
}

RERANK_DEFAULTS = {
    "rerankPoolSize": 40,    # how many top fused candidates get sent to the reranker
    "rerankMaxChars": 2000,  # passage text is truncated to this many chars before reranking
    "rerankWeight": 1.0,     # 1.0 = reranker score replaces the fused score; 0.0 = reranker ignored
}

DEFAULTS = {
    "rrfK": 60,                # RRF smoothing constant
    "vecGap": 0.20,            # drop vector-only hits this far below the best cosine sim
    "ftsKeep": 5,              # always keep the top-N BM25 hits regardless of vector score
    "anchorCount": 5,          # only expand context around the top-N ranked candidates
    "symbolAnchorLimit": 25,   # max chunks returned by symbol-name lookup
    "maxSiblingPieces": 6,     # max pieces pulled in per expanded symbol
    "siblingDiscount": 0.8,    # score multiplier for sibling pieces of an anchor
    "parentDiscount": 0.6,     # score multiplier for the parent's header chunk
    "pathPenalty": 0.5,        # score multiplier for tests/docs/readme paths
    "maxContainerLines": 150,  # nested-range dedupe: keep container only if <= this
}
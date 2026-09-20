[1/7] Opening SQLite DB at /home/usatkr/.coreIndexer/.codebase_index/index.sqlite ...
  Tables found: ['chunk_tags', 'chunks', 'code_snippets', 'code_snippets_tags', 'fts', 'fts_config', 'fts_content', 'fts_data', 'fts_docsize', 'fts_idx', 'fts_metadata', 'global_cache', 'indexing_lock', 'lance_db_cache', 'sqlite_sequence', 'symbols', 'tag_catalog']
[2/7] Initializing embeddings provider ...
  Embedding model: sentence-transformers::jinaai/jina-embeddings-v2-base-code
[3/7] Initializing FullTextSearchCodebaseIndex ...
[4/7] Initializing LanceDbIndex ...
[5/7] Building NoRerankerRetrievalPipeline and RerankerRetrievalPipeline
[6/7] Initializing Reranker provider ...
  Reranker model: sentence-transformers::jinaai/jina-reranker-v3
[7/7] Running 5 queries ...

  Tags: [BranchAndDir(directory='file:///home/usatkr/u_ml/projects/CoreIndexer', branch='retrival')]

==============================================================================
[res1] Explain how chunkCodebaseIndex work?
==============================================================================

  RANK  Δ     SCORE    SOURCE                       PATH
  ----- ----- -------- ---------------------------- ----------------------------------------
  0     =     0.0599   fts,symbol_match,vector      …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  1     =     0.0581   fts,symbol_child,vector      …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  2     =     0.0427   fts,symbol_child             …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  3     =     0.0421   fts,symbol_child             …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  4     =     0.0414   fts,symbol_child             …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  5     =     0.0401   fts,symbol_child             …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  6     =     0.0294   symbol_child                 …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  7     =     0.0290   symbol_child                 …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  8     =     0.0286   symbol_child                 …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  9     =     0.0245   fts,vector                   …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  10    =     0.0240   fts,vector                   …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  11    =     0.0161   vector                       …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  12    =     0.0154   vector                       …ml/projects/CoreIndexer/fts/fullTextSearchCodebaseIndex.py
  13    =     0.0152   vector                       /home/usatkr/u_ml/projects/CoreIndexer/base/index_d.py

  ✓ target 'ChunkCodebaseIndex':  RRF=#0  RR+rerank=#0

==============================================================================
[res2] how is retrieval working ??
==============================================================================

  RANK  Δ     SCORE    SOURCE                       PATH
  ----- ----- -------- ---------------------------- ----------------------------------------
  0     =     0.0164   vector                       …me/usatkr/u_ml/projects/CoreIndexer/retrieval/retrieval.py
  1     =     0.0161   vector                       …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  2     =     0.0159   vector                       …/u_ml/projects/CoreIndexer/retrieval/no_rerank_pipeline.py
  3     =     0.0156   vector                       /home/usatkr/u_ml/projects/CoreIndexer/base/index_types.py
  4     =     0.0154   vector                       …ml/projects/CoreIndexer/fts/fullTextSearchCodebaseIndex.py
  5     =     0.0149   vector                       …ml/projects/CoreIndexer/fts/fullTextSearchCodebaseIndex.py
  6     =     0.0147   vector                       …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  7     =     0.0145   vector                       …tkr/u_ml/projects/CoreIndexer/retrieval/rerank_pipeline.py
  8     =     0.0141   vector                       …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  9     =     0.0137   vector                       …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  10    =     0.0128   vector                       …u_ml/projects/CoreIndexer/codesnippet/codeSnippetsIndex.py

  ✓ target 'BaseRetrievalPipeline':  RRF=#miss  RR+rerank=#miss

==============================================================================
[res3] how are chunks stored in sqlite
==============================================================================

  RANK  Δ     SCORE    SOURCE                       PATH
  ----- ----- -------- ---------------------------- ----------------------------------------
  0     =     0.0241   fts,vector                   …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  1     =     0.0231   fts,vector                   …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  2     =     0.0231   fts,vector                   …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  3     =     0.0222   fts,vector                   …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  4     =     0.0220   fts,vector                   …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  5     =     0.0216   fts,vector                   …ml/projects/CoreIndexer/fts/fullTextSearchCodebaseIndex.py
  6     =     0.0215   fts,vector                   …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  7     =     0.0212   fts,vector                   …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  8     =     0.0210   fts,vector                   …/usatkr/u_ml/projects/CoreIndexer/lance_db/lanceDbIndex.py
  9     =     0.0209   fts,vector                   …kr/u_ml/projects/CoreIndexer/chunker/chunkCodebaseIndex.py
  10    =     0.0206   fts,vector                   …tkr/u_ml/projects/CoreIndexer/retrieval/rerank_pipeline.py
  11    =     0.0154   vector                       /home/usatkr/u_ml/projects/CoreIndexer/base/index_d.py

  ✓ target 'insert_chunks':  RRF=#miss  RR+rerank=#miss

==============================================================================
[res4] where does the FTS index get updated
==============================================================================

  RANK  Δ     SCORE    SOURCE                       PATH
  ----- ----- -------- ---------------------------- ----------------------------------------
  0     =     0.0260   fts,vector                   …satkr/u_ml/projects/CoreIndexer/indexer/codeBaseIndexer.py
  1     =     0.0252   fts,vector                   /home/usatkr/u_ml/projects/CoreIndexer/base/index_types.py
  2     =     0.0252   fts,symbol_expansion,vector  /home/usatkr/u_ml/projects/CoreIndexer/base/refresh_index.py
  3     =     0.0249   fts,vector                   …ml/projects/CoreIndexer/fts/fullTextSearchCodebaseIndex.py
  4     =     0.0248   fts,vector                   …satkr/u_ml/projects/CoreIndexer/indexer/codeBaseIndexer.py
  5     =     0.0245   fts,vector                   …ml/projects/CoreIndexer/fts/fullTextSearchCodebaseIndex.py
  6     =     0.0241   fts,vector                   …ml/projects/CoreIndexer/fts/fullTextSearchCodebaseIndex.py
  7     =     0.0223   fts,vector                   …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  8     =     0.0126   fts,vector                   /home/usatkr/u_ml/projects/CoreIndexer/test4.py
  9     =     0.0126   fts,vector                   /home/usatkr/u_ml/projects/CoreIndexer/docs/tables_schema.md
  10    =     0.0117   fts,vector                   /home/usatkr/u_ml/projects/CoreIndexer/test2.py
  11    =     0.0109   fts                          /home/usatkr/u_ml/projects/CoreIndexer/base/refresh_index.py

  ✓ target 'FullTextSearchCodebaseIndex.update':  RRF=#miss  RR+rerank=#miss

==============================================================================
[res5] how does hybrid scoring work
==============================================================================

  RANK  Δ     SCORE    SOURCE                       PATH
  ----- ----- -------- ---------------------------- ----------------------------------------
  0     =     0.0164   vector                       …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  1     =     0.0159   vector                       …me/usatkr/u_ml/projects/CoreIndexer/retrieval/rerankers.py
  2     =     0.0156   vector                       …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  3     =     0.0154   vector                       …me/usatkr/u_ml/projects/CoreIndexer/retrieval/rerankers.py
  4     =     0.0149   vector                       /home/usatkr/u_ml/projects/CoreIndexer/retrieval/models.py
  5     =     0.0147   vector                       …tkr/u_ml/projects/CoreIndexer/retrieval/rerank_pipeline.py
  6     =     0.0145   vector                       …tkr/u_ml/projects/CoreIndexer/retrieval/rerank_pipeline.py
  7     =     0.0141   vector                       …/u_ml/projects/CoreIndexer/retrieval/no_rerank_pipeline.py
  8     =     0.0139   vector                       …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  9     =     0.0137   vector                       …me/usatkr/u_ml/projects/CoreIndexer/retrieval/rerankers.py
  10    =     0.0133   vector                       …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py
  11    =     0.0132   vector                       …satkr/u_ml/projects/CoreIndexer/retrieval/base_pipeline.py

  ✓ target '_fuse_weighted':  RRF=#11  RR+rerank=#11

==============================================================================
Done.
==============================================================================

"""
CapyIdx — index a codebase, look up symbols by name.

The public surface is re-exported here so consumers can write::

    from capyidx import index_repo, lookup_symbol, resolve_lookup

rather than reaching into submodules. Everything documented in
:mod:`capyidx.api` is available from the package root.
"""
from __future__ import annotations


from capyidx.api import (
    index_repo,
    index_repo_iter,
    lookup_symbol,
    open_lookup,
    resolve_lookup,
    reconstruct_symbol,
    PathLike,
    ResolvedLookup,
)
from capyidx.base.index_d import (
    Chunk,
    Chonk,
    ChunkingResult,
    Symbol,
    BranchAndDir
)
from capyidx.retrieval.models import (
    LookupResult,
    SymbolCode,
    SymbolMatch,
)

__version__ = "0.1.0"

__all__ = [
    # Types
    "PathLike",
    "__version__",
    # Indexing
    "index_repo",
    "index_repo_iter",
    # Querying
    "open_lookup",
    "lookup_symbol",
    "resolve_lookup",
    "reconstruct_symbol",
    "ResolvedLookup",
    # Result types
    "LookupResult",
    "SymbolCode",
    "SymbolMatch",
    # Core data types
    "Chunk",
    "Chonk",
    "ChunkingResult",
    "Symbol",
    "BranchAndDir"
]
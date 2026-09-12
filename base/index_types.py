from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    Optional,
    Protocol,
    TypedDict,
)

from index_d import IndexTag, IndexingProgressUpdate




# ---------------------------------------------------------------------------
# IndexResultType  — KEEP THE STRING VALUES. They are persisted / matched.
# ---------------------------------------------------------------------------

class IndexResultType(str, Enum):
    COMPUTE = "compute"
    DELETE = "delete"                  
    ADD_TAG = "addTag"
    REMOVE_TAG = "removeTag"
    UPDATE_LAST_UPDATED = "updateLastUpdated"


# ---------------------------------------------------------------------------
# PathAndCacheKey / RefreshIndexResults
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PathAndCacheKey:
    path: str
    cache_key: str 


@dataclass(slots=True)
class RefreshIndexResults:
    compute: list[PathAndCacheKey] = field(default_factory=list)
    delete: list[PathAndCacheKey] = field(default_factory=list)      
    add_tag: list[PathAndCacheKey] = field(default_factory=list)     
    remove_tag: list[PathAndCacheKey] = field(default_factory=list)  



# ---------------------------------------------------------------------------
# Callbacks / function types
# ---------------------------------------------------------------------------

MarkCompleteCallback = Callable[
    [list[PathAndCacheKey], IndexResultType],
    Awaitable[None],
]


RefreshIndex = Callable[[IndexTag], Awaitable[RefreshIndexResults]]


# ---------------------------------------------------------------------------
# One index backend (chunk / FTS / embeddings / snippets)
# ---------------------------------------------------------------------------

class CodebaseIndex(Protocol):
    artifact_id: str                 
    relative_expected_time: float    

    def update(
        self,
        tag: IndexTag,
        results: RefreshIndexResults,
        mark_complete: MarkCompleteCallback,
        repo_name: Optional[str],   
    ) -> AsyncIterator[IndexingProgressUpdate]:
        ...

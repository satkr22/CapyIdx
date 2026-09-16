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

from base.index_d import IndexTag, IndexingProgressUpdate, FileSystem




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


@dataclass
class IndexContext:
    filesystem: FileSystem
    repo_name: Optional[str] = None

# ---------------------------------------------------------------------------
# One index backend (chunk / FTS / embeddings / snippets)
# ---------------------------------------------------------------------------

class CodebaseIndexer(Protocol):
    artifact_id: str                 
    relative_expected_time: float    

    def update(
        self,
        tag: IndexTag,
        context: IndexContext,
        results: RefreshIndexResults,
        mark_complete: MarkCompleteCallback,
        # repo_name: Optional[str],   
    ) -> AsyncIterator[IndexingProgressUpdate]:
        ...

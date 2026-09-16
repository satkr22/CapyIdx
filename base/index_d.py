"""Python types from Continue core/index.d.ts, indexer-only.

Dropped: Window, ILLM, chat, MCP, tools, slash commands, config, sessions, LSP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Literal, Optional, Protocol, Tuple, List, Dict


# --- chunks ---

@dataclass
class ChunkWithoutID:
    content: str
    start_line: int
    end_line: int
    signature: Optional[str] = None
    other_metadata: Optional[dict[str, Any]] = None


@dataclass
class Chunk(ChunkWithoutID):
    digest: str = ""
    filepath: str = ""
    index: int = 0


# --- indexing progress ---

IndexingStatus = Literal[
    "loading",
    "waiting",
    "indexing",
    "done",
    "failed",
    "paused",
    "disabled",
    "cancelled",
]


@dataclass
class IndexingProgressUpdate:
    progress: float
    desc: str
    status: IndexingStatus
    should_clear_indexes: Optional[bool] = None
    debug_info: Optional[str] = None
    warnings: Optional[list[str]] = None


# --- which indexes to run ---

ContextIndexingType = Literal[
    "chunk",
    "embeddings",
    "full_text_search",
    "code_snippets",
]

# -- fts --

@dataclass(frozen=True)
class BranchAndDir:
    directory: str
    branch: str


@dataclass
class RetrieveConfig:
    tags: list[BranchAndDir]
    text: str
    n: int
    directory: Optional[str] = None
    filter_paths: Optional[list[str]] = None
    bm25_threshold: Optional[float] = None

# --- positions (snippets / ranges) ---

@dataclass
class Position:
    line: int
    character: int


@dataclass
class Range:
    start: RangePosition
    end: RangePosition


@dataclass
class RangeInFile:
    filepath: str
    range: Range


# --- tag = (workspace, branch, artifact) ---

@dataclass(frozen=True)
class IndexTag:
    directory: str
    branch: str
    artifact_id: str


class FileType(IntEnum):
    UNKNOWN = 0
    FILE = 1
    DIRECTORY = 2
    SYMBOLIC_LINK = 64


@dataclass(frozen=True)
class FileStats:
    size: int
    last_modified: int


@dataclass
class RangePosition:
    line: int
    character: int

@dataclass
class SymbolWithRange:
    filepath: str
    type: str
    name: str
    range: Range
    content: str
    
FileSymbolMap = Dict[str, List[SymbolWithRange]]

FileStatsMap = dict[str, FileStats]


# --- filesystem the indexer actually calls (replaces IDE) ---

class FileSystem(Protocol):
    async def read_file(self, uri: str) -> str: ...

    async def file_exists(self, uri: str) -> bool: ...

    async def list_dir(self, uri: str) -> List[Tuple[str, FileType]]: ...

    async def get_file_stats(self, uris: list[str]) -> FileStatsMap: ...

    async def get_workspace_dirs(self) -> list[str]: ...

    async def get_branch(self, directory_uri: str) -> str: ...

    async def get_repo_name(self, directory_uri: str) -> Optional[str]: ...

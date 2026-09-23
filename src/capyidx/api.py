"""
Public API for CapyIdx.

Two workflows:

* **Index** — walk a repo, chunk it, persist to SQLite.

  - ``index_repo`` — build the index once, discard progress, return.
  - ``index_repo_iter`` — same, but yield progress updates. Pass
    ``watch=True`` to keep yielding after the initial pass as files
    change on disk.

* **Query** — look up symbols against an existing index. Four entry
  points, cheapest to most convenient:

  - ``open_lookup`` — context manager yielding a raw ``SymbolLookup``
    bound to one SQLite connection. Use it when you're making several
    calls against the same repo, or when you need behaviour the
    one-shot helpers don't expose. Connection is opened once and reused
    for the duration of the ``async with`` block.

  - ``lookup_symbol`` — one name in, one ``LookupResult`` out.
    Metadata only: ``matches`` (id, name, type, path, line range,
    match kind), plus ``selected`` populated *only* when there is a
    unique exact match. Nothing is reconstructed unless the lookup
    pipeline already had to. Use this for pick-lists, autocomplete,
    "does this symbol exist?" probes.

  - ``reconstruct_symbol`` — one ``symbol_id`` in, one ``SymbolCode``
    out. Use this on the second half of a pick-list flow: the user
    chose a match, now you fetch its body. Do *not* call this in a
    loop — use ``resolve_lookup`` or ``open_lookup`` instead.

  - ``resolve_lookup`` — one name in, ``ResolvedLookup`` out, which
    carries both the ``LookupResult`` (for metadata) and a ``codes``
    list containing a reconstructed ``SymbolCode`` for *every* match.
    This is the answer to "``selected`` is ``None`` because there are
    multiple matches" — instead of looping yourself, you get all the
    bodies in one round-trip. Order of ``codes`` matches order of
    ``matches``.

Typical flows:

* *Just show me what exists* — ``lookup_symbol``.
* *User picked one, give me its body* — ``lookup_symbol`` then
  ``reconstruct_symbol``.
* *Dump every definition of this name* — ``resolve_lookup``.
* *I'm a long-lived tool doing many lookups* — ``open_lookup``.

All query entry points accept ``filter_paths`` to restrict work to
symbols under given path prefixes; the name-based ones also accept
``detail`` (``"body"`` or ``"signature"``), ``include_children``, and
``max_lines``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Union, List, Tuple, Optional, Sequence

from capyidx.base.index_d import BranchAndDir
from capyidx.base.index_types import IndexingProgressUpdate
from capyidx.chunker.chunk_codebase_index import ChunkCodebaseIndex
from capyidx.db.db import open_index
from capyidx.indexer.codebase_indexer import CodeIndexer
from capyidx.retrieval.models import LookupResult, SymbolCode, ResolvedLookup
from capyidx.retrieval.retrieval_pipeline import SymbolLookup
from capyidx.utils.disk_operations import DiskOperations
from capyidx.utils.retrieval_utils import get_current_tags


PathLike = Union[str, Path]


# ---------------------------------------------------------------------------
# Internal setup
# ---------------------------------------------------------------------------

async def _setup(
    repo: PathLike,
) -> Tuple[DiskOperations, List[str], BranchAndDir]:
    """Resolve filesystem, workspace roots, and tags for a repository.
    """
    repo_path = Path(repo).resolve()

    if not repo_path.exists():
        raise FileNotFoundError(f"repo path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise NotADirectoryError(f"repo path is not a directory: {repo_path}")

    fs = DiskOperations(roots=[str(repo_path)])
    roots = await fs.get_workspace_dirs()

    tags_list = get_current_tags(roots)
    if not tags_list:
        raise RuntimeError(
            f"could not determine tags for repo: {repo_path}. "
            f"Is it a git repository?"
        )

    return fs, roots, tags_list[0]


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

async def index_repo(
    repo: PathLike,
    *,
    max_chunk_size: int = 512,
) -> None:
    """Index a repository once, waiting for completion.

    Convenience wrapper around :func:`index_repo_iter` that discards
    progress updates. Use this in scripts and CLIs where you just want
    the index built. For progress reporting, use ``index_repo_iter``.

    Args:
        repo: Path to the repository root.
        max_chunk_size: Approximate token budget per chunk.

    Raises:
        FileNotFoundError: If ``repo`` does not exist.
        NotADirectoryError: If ``repo`` is not a directory.
        RuntimeError: If ``repo`` is not a git repository.
    """
    async for _ in index_repo_iter(repo, max_chunk_size=max_chunk_size):
        pass
    print("Indexing Completed.")


async def index_repo_iter(
    repo: PathLike,
    *,
    watch: bool = False,
    max_chunk_size: int = 512,
    flush_interval: float = 5.0,
) -> AsyncIterator[IndexingProgressUpdate]:
    """Index a repository, yielding progress updates as it works.

    With ``watch=False`` (default), the iterator ends when the initial
    indexing pass completes. With ``watch=True``, after the initial pass
    the iterator keeps yielding updates as files change. The caller stops
    watching by breaking out of the loop or cancelling the consuming task.

    Args:
        repo: Path to the repository root.
        watch: If True, continue watching for file changes after the
            initial index pass.
        max_chunk_size: Approximate token budget per chunk.
        flush_interval: Watch-mode debounce in seconds. Ignored when
            ``watch`` is False.

    Yields:
        :class:`IndexingProgressUpdate` for each phase of work.

    Example:
        >>> async for update in index_repo_iter("/path/to/repo"):
        ...     print(f"[{update.status}] {update.progress:.1%} {update.desc}")

        >>> async for update in index_repo_iter("/path/to/repo", watch=True):
        ...     print(f"[watch] {update.desc}")
    """
    fs, roots, tags = await _setup(repo)
    conn = open_index(tags)

    try:
        chunk_index = ChunkCodebaseIndex(
            db=conn,
            filesystem=fs,
            max_chunk_size=max_chunk_size,
        )
        indexer = CodeIndexer(fs=fs, indexes=[chunk_index])

        async for update in indexer.refresh_codebase_index(roots, conn):
            yield update
        print("Indexing Completed.")

        if watch:
            async for update in indexer.start_watch(
                db=conn,
                workspace_dirs=roots, 
                flush_interval=flush_interval
            ):
                yield update
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

@asynccontextmanager
async def open_lookup(
    repo: PathLike,
) -> AsyncGenerator[SymbolLookup, None]:
    """Open a symbol lookup session for a repository.

    The returned :class:`SymbolLookup` holds an open SQLite connection
    for the duration of the ``async with`` block. Use this when you plan
    to make multiple lookups against the same index — the connection is
    opened once and reused.

    Args:
        repo: Path to the repository root.

    Yields:
        A :class:`SymbolLookup` bound to the repo's index.

    Example:
        >>> async with open_lookup("/path/to/repo") as lk:
        ...     a = lk.lookup("SymbolLookup", detail="signature")
        ...     b = lk.lookup("ChunkCodebaseIndex", detail="body")
    """
    fs, roots, tags = await _setup(repo)
    conn = open_index(tags)

    try:
        yield SymbolLookup(conn, roots)
    finally:
        conn.close()


async def lookup_symbol(
    repo: PathLike,
    name: str,
    *,
    detail: Literal["signature", "body"] = "body",
    symbol_id: str | None = None,
    filter_paths: Optional[Sequence[str]] = None,
    include_children: bool = True,
    max_lines: int = 0,
) -> LookupResult:
    """One-shot symbol lookup.

    Opens the index, runs a single query, closes the index, returns the
    result. For a batch of lookups, use :func:`open_lookup` to reuse the
    connection across calls.

    Args:
        repo: Path to the repository root.
        name: Symbol name to search for. Case-sensitive exact, then
            case-insensitive exact, then case-insensitive substring.
        detail: ``"body"`` returns the reconstructed code;
            ``"signature"`` returns only declaration headers.
        symbol_id: If the query has multiple matches, pass the id of the
            one to reconstruct. Ignored when there is a unique match.
        filter_paths: Only consider symbols whose ``path`` starts with one of
            these prefixes. ``None`` means no filtering.
        include_children: If True, a class/file symbol also includes its
            descendants in the reconstruction tree.
        max_lines: Truncate the reconstructed body to this many lines.
            ``0`` means no limit.

    Returns:
        :class:`LookupResult` with ``matches``, optionally ``selected``,
        and ``limitations``.

    Example:
        >>> result = await lookup_symbol(
        ...     "/path/to/repo", "SymbolLookup", detail="signature"
        ... )
        >>> for m in result.matches:
        ...     print(f"{m.type:8} {m.name:30} {m.path}:{m.start_line}-{m.end_line}")
    """
    async with open_lookup(repo) as lk:
        return lk.lookup(name, symbol_id=symbol_id, detail=detail, filter_paths=filter_paths, include_children=include_children, max_lines=max_lines)




async def reconstruct_symbol(
    repo: PathLike,
    symbol_id: str,
    *,
    detail: Literal["signature", "body"] = "body",
    filter_paths: Optional[Sequence[str]] = None,
) -> SymbolCode:
    """One-shot reconstruction of a single symbol by id.

    Companion to :func:`lookup_symbol`. Use this when you already have a
    ``SymbolMatch.id`` (e.g. from a previous ``LookupResult.matches``)
    and want just that symbol's code.

    Args:
        repo: Path to the repository root.
        symbol_id: The ``SymbolMatch.id`` to reconstruct.
        detail: ``"body"`` for full code, ``"signature"`` for headers.
        filter_paths: Only consider symbols whose ``path`` starts with one 
            of these prefixes. ``None`` means no filtering.

    Returns:
        The reconstructed :class:`SymbolCode`.
        
    Example:
            >>> result = await lookup_symbol(
            ...     "/path/to/repo", "SymbolLookup", detail="signature"
            ... )
            >>> for m in result.matches:
            ...     print(f"{m.type:8} {m.name:30} {m.path}:{m.start_line}-{m.end_line}")
            
            >>>     symbol_code = await reconstruct_symbol(
            ...         "/path/to/repo", m.id, detail="signature"
            ...     )
            ...     print(f"Code:\n{symbol_code.code}")
    
    """
    async with open_lookup(repo) as lk:
        return lk.reconstruct(symbol_id, detail=detail, filter_paths=filter_paths)




async def resolve_lookup(
    repo: PathLike,
    name: str,
    *,
    detail: Literal["signature", "body"] = "body",
    symbol_id: str | None = None,
    filter_paths: Optional[Sequence[str]] = None,
    include_children: bool = True,
    max_lines: int = 0,
) -> ResolvedLookup:
    """Look up a symbol and reconstruct code for *every* match.

    This is the fix for the "multiple matches : ``selected`` is None"
    problem: instead of making the caller loop over ``matches`` and call
    :meth:`SymbolLookup.reconstruct` themselves, this returns the bodies
    in one shot.
    
    Args:
        repo: Path to the repository root.
        name: Symbol name to search for. Case-sensitive exact, then
            case-insensitive exact, then case-insensitive substring.
        detail: ``"body"`` for full code, ``"signature"`` for headers.
        symbol_id: The ``SymbolMatch.id`` to reconstruct.
        filter_paths: Only consider symbols whose ``path`` starts with one 
            of these prefixes. ``None`` means no filtering.
        include_children: If True, a class/file symbol also includes its
            descendants in the reconstruction tree.
        max_lines: Truncate the reconstructed body to this many lines.
            ``0`` means no limit.
    
        Returns:
            The reconstructed :class:`SymbolCode`.

    Behaviour:

    * Unique exact match → ``codes`` contains just that symbol.
    * No unique match (multiple matches, or only case-insensitive /
      substring hits) → ``codes`` contains the reconstruction of every
      entry in ``result.matches``, in the same order.
    * No matches at all → ``codes`` is empty; ``result.limitations``
      explains why.

    Individual reconstruct failures are swallowed so one broken symbol
    doesn't nuke the whole batch — the corresponding ``SymbolMatch`` is
    still present in ``result.matches`` for diagnostics.

    Example:
        >>> r = await resolve_lookup("/path/to/repo", "SymbolLookup")
        >>> for code in r.codes:
        ...     print(code.path, code.start_line, "->", len(code.code), "lines")
        ...     print(f"Code:\n{code.code}")
    """
    async with open_lookup(repo) as lk:
        result = lk.lookup(name=name, detail=detail, filter_paths=filter_paths, symbol_id=symbol_id, include_children=include_children, max_lines=max_lines)

        codes: list[SymbolCode] = []

        if result.selected is not None:
            codes.append(result.selected)
            
        elif symbol_id is not None:
            codes.append(
                await asyncio.to_thread(
                    lk.reconstruct, symbol_id=symbol_id, detail=detail, filter_paths=filter_paths
                )
            )
            
        elif result.matches:
            for match in result.matches:
                try:
                    codes.append(
                        await asyncio.to_thread(
                            lk.reconstruct, symbol_id=match.id, detail=detail, filter_paths=filter_paths
                        )
                    )
                except (KeyError, ValueError) as exc:
                    # Leave the match in result.matches for the caller to
                    # inspect; skip just the reconstruction.
                    continue

        return ResolvedLookup(result=result, codes=codes)


__all__ = [
    "index_repo",
    "index_repo_iter",
    "open_lookup",
    "lookup_symbol",
    "reconstruct_symbol",
    "resolve_lookup",
    "ResolvedLookup",
    "PathLike"
]
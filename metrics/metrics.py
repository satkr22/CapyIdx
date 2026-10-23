"""Measure CapyIdx indexing time for a repository.

Usage:

    1. Set REPO_PATH below, then run python metrics.py; or
    2. Pass a repository path directly:

       python metrics.py /path/to/repository

The file count is read from CapyIdx's persisted file symbols, so it is
the number of files that were actually indexed rather than the number of
files that happen to exist on disk.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent

_SRC_DIR = _PROJECT_ROOT / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from capyidx.api import index_repo_iter
from capyidx.db.db import open_index
from capyidx.db.paths import db_path_for
from capyidx.utils.retrieval_utils import get_current_tags
from capyidx.utils.uri1 import get_path_to_uri


# Set this to a repository path if you want to run ``python metrics.py``
# without passing a command-line argument.
REPO_PATH: str | Path | None = None


def _count_indexed_files(repo: Path) -> tuple[int, int]:
    """Return the number of distinct files and chunks stored in the repository index."""
    tags = get_current_tags([get_path_to_uri(str(repo))])
    if not tags:
        return 0, 0

    conn = open_index(tags[0], db_path=db_path_for(tags[0]))
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT path) FROM symbols WHERE type = 'file'"
        ).fetchone()
        
        chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks"
        ).fetchone()
        
        return int(row[0]) if row else 0, int(chunks[0]) if chunks else 0
    finally:
        conn.close()


async def _measure(repo: Path, max_chunk_size: int) -> tuple[int, int, float]:

    started = time.perf_counter()

    async for update in index_repo_iter(repo, max_chunk_size=max_chunk_size):
        if update.status == "failed":
            raise RuntimeError(update.desc)

    elapsed = time.perf_counter() - started
    
    file_count, chunks_count = _count_indexed_files(repo)
    
    return file_count, chunks_count, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index a repository and report the indexed file count and elapsed time."
    )
    parser.add_argument(
        "repo",
        nargs="?",
        help="path to the repository (overrides REPO_PATH)",
    )
    parser.add_argument(
        "--max-chunk-size",
        type=int,
        default=512,
        help="approximate token budget per chunk (default: 512)",
    )
    args = parser.parse_args()

    configured_path = args.repo if args.repo is not None else REPO_PATH
    if configured_path is None:
        parser.error(
            "provide a repository path or set REPO_PATH near the top of metrics.py"
        )

    repo = Path(configured_path).expanduser().resolve()
    try:
        file_count, chunks_count, elapsed = asyncio.run(_measure(repo, args.max_chunk_size))
    except (FileNotFoundError, NotADirectoryError, RuntimeError) as exc:
        parser.error(str(exc))

    print(f"Repository: {repo}")
    print(f"Files indexed: {file_count}")
    print(f"Chunks created: {chunks_count}")
    print(f"Time taken: {elapsed:.2f} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Keep an index updated while source files change.

Stop with Ctrl-C::

    python examples/watch_repository.py /path/to/repository
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import capyidx


async def main(repository: Path) -> None:
    repository = repository.resolve()
    os.chdir(repository)
    async for update in capyidx.index_repo_iter(
        repository,
        watch=True,
        flush_interval=2.0,
    ):
        print(f"[{update.status}] {update.progress:.0%} {update.desc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.repository))
    except KeyboardInterrupt:
        print("\nStopped watching.")

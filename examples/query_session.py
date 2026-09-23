"""Reuse one lookup connection for several queries.

Index the repository first, for example with ``examples/sample.py``::

    python examples/query_session.py /path/to/repository MyClass OtherClass
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import capyidx


async def main(repository: Path, symbols: list[str]) -> None:
    repository = repository.resolve()
    os.chdir(repository)
    async with capyidx.open_lookup(repository) as lookup:
        for name in symbols:
            result = lookup.lookup(name, detail="signature")
            print(f"{name!r}: {len(result.matches)} match(es)")
            for match in result.matches:
                print(f"  {match.type:8} {match.path}:{match.start_line}-{match.end_line}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("symbols", nargs="+", help="Symbol names to look up")
    args = parser.parse_args()
    asyncio.run(main(args.repository, args.symbols))

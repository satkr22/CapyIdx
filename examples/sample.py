"""Small end-to-end CapyIdx example.

Run this after installing the package::

    python examples/sample.py /path/to/repository MyClass

The repository may be a Git checkout or an ordinary source directory.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import capyidx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("symbol", help="Exact or partial symbol name")
    parser.add_argument(
        "--detail",
        choices=("body", "signature"),
        default="body",
        help="Render the symbol body or declaration signature",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=0,
        help="Limit reconstructed output; zero means unlimited",
    )
    parser.add_argument(
        "--filter-path",
        action="append",
        default=None,
        help="Restrict lookup to a stored path; may be repeated",
    )
    return parser.parse_args()


async def main(
    repository: Path,
    symbol: str,
    detail: str,
    max_lines: int,
    filter_paths: list[str] | None,
) -> None:
    repository = repository.resolve()
    os.chdir(repository)
    print(f"Indexing {repository}...")
    async for update in capyidx.index_repo_iter(repository):
        if update.status in {"indexing", "done"}:
            print(f"  {update.status}: {update.progress:.0%} {update.desc}")

    result = await capyidx.lookup_symbol(
        repository,
        symbol,
        detail=detail, # type: ignore
        filter_paths=filter_paths,
        max_lines=max_lines,
    )

    if result.selected is not None:
        print("Exact symbol mathced:")
        print(f"{result.selected.type:8} {result.selected.path}:{result.selected.start_line}-{result.selected.end_line}")
        print(f"Code:\n", result.selected.code)
        return
    
    if not result.matches:
        print(f"No symbols matched {symbol!r}.")
        return
    
    if result.selected is None:
        print("More than one symbol matched; resolving every match.")
        resolved = await capyidx.resolve_lookup(
            repository,
            symbol,
            detail=detail, # type: ignore
            filter_paths=filter_paths,
            max_lines=max_lines,
        )
        for code in resolved.codes:
            print(f"\n--- {code.path}:{code.start_line}-{code.end_line} ---\nCode:\n{code.code}")
        return
    
    

    print(f"Found {len(result.matches)} match(es):")
    for index, match in enumerate(result.matches, start=1):
        print(
            f"  {index}. {match.name} ({match.type}) "
            f"{match.path}:{match.start_line}-{match.end_line}"
        )


    print(f"\n--- selected {result.selected.path}:{result.selected.start_line} ---")
    print(result.selected.code)


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(
        main(
            arguments.repository,
            arguments.symbol,
            arguments.detail,
            arguments.max_lines,
            arguments.filter_path,
        )
    )

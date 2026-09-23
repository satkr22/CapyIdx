# CapyIdx

CapyIdx indexes source repositories into SQLite and provides deterministic
symbol lookup and code reconstruction. It is designed to be embedded in tools
that need repository-aware code context. It has incremental re-indexing feature
that re-indexes only modified  or deleted (deteles indexes of deleted file) files.
It looks for files which are modified and after a set interval(default=5 sec) it re-indexes them.

## Installation

```bash
python -m pip install capyidx
```

For development:

```bash
python -m pip install -e ".[dev]"
```

The core package uses Tree-sitter for source parsing and `tiktoken` for chunk
size calculations. Optional integrations can be installed with extras:

```bash
python -m pip install "capyidx[openai]"
python -m pip install "capyidx[ollama]"
python -m pip install "capyidx[local-embeddings]"
python -m pip install "capyidx[vector]"
```

## Basic usage

Indexing is asynchronous. The convenience API waits for the initial pass to
finish:

```python
import asyncio
from capyidx import index_repo, lookup_symbol


async def main() -> None:
    repo = "/path/to/git/repository"
    await index_repo(repo)

    result = await lookup_symbol(repo, "MyClass", detail="body")
    for match in result.matches:
        print(match.name, match.path, match.start_line, match.end_line)

    if result.selected is not None:
        print(result.selected.code)


asyncio.run(main())
```

Use `index_repo_iter` when progress updates are needed:

```python
from capyidx import index_repo_iter

async for update in index_repo_iter("/path/to/git/repository"):
    print(update.status, update.progress, update.desc)
```

Pass `watch=True` to continue indexing changed files after the initial pass.
The watcher uses `watchdog`(recommended) when installed and otherwise falls back to polling.

## Query workflows

- `lookup_symbol` returns matching symbol metadata and reconstructs a unique
  exact match.
- `reconstruct_symbol` reconstructs a previously selected symbol by ID.
- `resolve_lookup` reconstructs every match for a name.
- `open_lookup` reuses one database connection for multiple queries.

Use `detail="signature"` for declaration-only results, and use
`filter_paths` to restrict lookup to selected path prefixes.

## Examples

The [`examples/`](examples/) directory contains runnable scripts for the main
workflows:

```bash
# Index a repository and look up a symbol.
python examples/sample.py /path/to/repository MyClass --detail signature

# Reuse one lookup session for several symbol queries.
python examples/query_session.py /path/to/repository MyClass OtherClass

# Keep the index updated while files change.
python examples/watch_repository.py /path/to/repository
```

`sample.py` also demonstrates ambiguous matches, `resolve_lookup`,
`filter_paths`, and `max_lines`.

## Current scope

The public convenience API currently builds the chunk/symbol index and exposes
deterministic symbol retrieval. FTS, code-snippet, embedding, and LanceDB
backends are present as lower-level components but are not automatically wired
into `index_repo` yet.

Indexes are stored under `~/.capyidx/.codebase_index` by default. Set
`CAPYIDX_HOME` to use a different data directory.

## Development checks

```bash
python -m compileall -q src tests
pytest
python -m build
```
![CI](https://github.com/satkr22/CapyIdx/actions/workflows/ci.yml/badge.svg)
The repository's CI runs these checks on Python 3.10, 3.11, and 3.12.

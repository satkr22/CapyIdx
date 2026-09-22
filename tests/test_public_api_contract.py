from __future__ import annotations

import inspect

import coreindexer


def test_indexing_api_is_async() -> None:
    assert inspect.iscoroutinefunction(coreindexer.index_repo)
    assert inspect.isasyncgenfunction(coreindexer.index_repo_iter)


def test_lookup_api_is_async() -> None:
    assert inspect.iscoroutinefunction(coreindexer.lookup_symbol)
    assert inspect.iscoroutinefunction(coreindexer.reconstruct_symbol)
    assert inspect.iscoroutinefunction(coreindexer.resolve_lookup)


def test_version_is_defined() -> None:
    assert coreindexer.__version__ == "0.1.0"

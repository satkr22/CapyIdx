from __future__ import annotations

import inspect

import capyidx


def test_indexing_api_is_async() -> None:
    assert inspect.iscoroutinefunction(capyidx.index_repo)
    assert inspect.isasyncgenfunction(capyidx.index_repo_iter)


def test_lookup_api_is_async() -> None:
    assert inspect.iscoroutinefunction(capyidx.lookup_symbol)
    assert inspect.iscoroutinefunction(capyidx.reconstruct_symbol)
    assert inspect.iscoroutinefunction(capyidx.resolve_lookup)


def test_version_is_defined() -> None:
    assert capyidx.__version__ == "0.1.0"

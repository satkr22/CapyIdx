from __future__ import annotations

from importlib.resources import files

import coreindexer


def test_package_exports_documented_public_api() -> None:
    expected = {
        "index_repo",
        "index_repo_iter",
        "open_lookup",
        "lookup_symbol",
        "reconstruct_symbol",
        "resolve_lookup",
        "LookupResult",
        "SymbolCode",
        "SymbolMatch",
        "ResolvedLookup",
    }

    assert expected <= set(coreindexer.__all__)
    for name in expected:
        assert hasattr(coreindexer, name)


def test_package_data_contains_tree_sitter_queries() -> None:
    query = files("coreindexer").joinpath("utils").joinpath(
        "tree_sitter_queries"
    ).joinpath("python.scm")

    assert query.is_file()

from __future__ import annotations

import pytest

from capyidx.mcp import format as fmt


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a.py", "python"),
        ("a.PYI", "python"),
        ("a.ts", "typescript"),
        ("a.tsx", "tsx"),
        ("a.js", "javascript"),
        ("a.jsx", "jsx"),
        ("a.mjs", "javascript"),
        ("a.cjs", "javascript"),
        ("a.go", "go"),
        ("a.rs", "rust"),
        ("a.java", "java"),
        ("a.kt", "kotlin"),
        ("a.scala", "scala"),
        ("a.rb", "ruby"),
        ("a.php", "php"),
        ("a.cs", "csharp"),
        ("a.cpp", "cpp"),
        ("a.cc", "cpp"),
        ("a.cxx", "cpp"),
        ("a.hpp", "cpp"),
        ("a.h", "c"),
        ("a.c", "c"),
        ("a.swift", "swift"),
        ("a.sh", "bash"),
        ("a.bash", "bash"),
        ("a.zsh", "bash"),
        ("a.sql", "sql"),
        ("a.yaml", "yaml"),
        ("a.yml", "yaml"),
        ("a.json", "json"),
        ("a.toml", "toml"),
        ("a.md", "markdown"),
        ("a.html", "html"),
        ("a.css", "css"),
        ("README", ""),
        ("a.unknown", ""),
    ],
)
def test_lang_mapping_is_case_insensitive_and_unknown_is_empty(
    path: str,
    expected: str,
) -> None:
    assert fmt._lang(path) == expected


def test_format_symbol_contains_metadata_and_language_fence() -> None:
    item = {
        "path": "src/example.py",
        "start_line": 4,
        "end_line": 8,
        "symbol_name": "example",
        "symbol_id": "id-1",
        "code": "def example():\n    return 1",
    }

    assert fmt.format_symbol(item) == (
        "src/example.py:4-8  symbol: example\n"
        "id: id-1\n"
        "```python\n"
        "def example():\n    return 1\n"
        "```"
    )


def test_format_symbol_uses_fallback_name_and_returned_end_for_truncation() -> None:
    item = {
        "path": "source.unknown",
        "start_line": 10,
        "end_line": 20,
        "returned_end_line": 12,
        "symbol_id": "id-2",
        "code": "part",
        "truncated": True,
        "total_lines": 11,
    }

    rendered = fmt.format_symbol(item)

    assert "source.unknown:10-12  symbol: ?" in rendered
    assert "[truncated — showing lines 10-12 of 11;" in rendered
    assert "```\npart\n```" in rendered


def test_format_symbol_error_does_not_require_symbol_fields() -> None:
    assert fmt.format_symbol({"__xx_e_code": "NOT_FOUND", "message": "missing"}) == (
        "[error NOT_FOUND] missing"
    )
    assert fmt._is_error({"__xx_e_code": "X", "message": "m"})
    assert not fmt._is_error({"__xx_e_code": "X"})
    assert not fmt._is_error("error")


def test_format_symbols_separates_multiple_items() -> None:
    items = [
        {
            "path": "a.py",
            "start_line": 1,
            "end_line": 1,
            "symbol_name": "a",
            "symbol_id": "a",
            "code": "a",
        },
        {"__xx_e_code": "E", "message": "bad"},
    ]

    rendered = fmt.format_symbols(items)

    assert "\n\n---\n\n" in rendered
    assert rendered.endswith("[error E] bad")


def test_format_lookup_renders_queries_signatures_and_names() -> None:
    result = {
        "foo": [
            {
                "path": "a.py",
                "start_line": 1,
                "end_line": 3,
                "symbol_id": "id-1",
                "symbol_name": "Foo",
                "signature": "def foo():\n    ...",
            },
            {"__xx_e_code": "E", "message": "nope"},
        ],
        "empty": [],
    }

    rendered = fmt.format_lookup(result)

    assert "query: foo" in rendered
    assert "- a.py:1-3  id=id-1  (Foo)" in rendered
    assert "def foo():\n    ..." in rendered
    assert "[error E] nope" in rendered
    assert "query: empty" in rendered


def test_format_lookup_list_errors_and_empty_mapping() -> None:
    assert fmt.format_lookup([{"__xx_e_code": "X", "message": "bad"}]) == (
        "[error X] bad"
    )
    assert fmt.format_lookup(["not a mapping"]) == "not a mapping"
    assert fmt.format_lookup({}) == "(no matches)"

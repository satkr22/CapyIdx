"""
Uses `tree_sitter` + `tree_sitter_language_pack`.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, cast

from tree_sitter import Language, Node, Parser, Query
from tree_sitter_language_pack import get_language as _get_language_by_name

from base.index_d import(
    Range,
    RangeInFile,
    SymbolWithRange,
    FileSymbolMap,
    RangePosition,
    FileSystem
)
from utils.disk_operations import DiskOperations
from utils.uri import get_uri_file_extension


# ---------------------------------------------------------------------------
# Language definitions
# ---------------------------------------------------------------------------


class LanguageName(str, Enum):
    CPP = "cpp"
    C_SHARP = "csharp"
    C = "c"
    CSS = "css"
    PHP = "php"
    BASH = "bash"
    JSON = "json"
    TYPESCRIPT = "typescript"
    TSX = "tsx"
    ELM = "elm"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    ELISP = "elisp"
    ELIXIR = "elixir"
    GO = "go"
    EMBEDDED_TEMPLATE = "embedded_template"
    HTML = "html"
    JAVA = "java"
    LUA = "lua"
    OCAML = "ocaml"
    QL = "ql"
    RESCRIPT = "rescript"
    RUBY = "ruby"
    RUST = "rust"
    SYSTEMRDL = "systemrdl"
    TOML = "toml"
    SOLIDITY = "solidity"


supported_languages: Dict[str, LanguageName] = {
    "cpp": LanguageName.CPP,
    "hpp": LanguageName.CPP,
    "cc": LanguageName.CPP,
    "cxx": LanguageName.CPP,
    "hxx": LanguageName.CPP,
    "cp": LanguageName.CPP,
    "hh": LanguageName.CPP,
    "inc": LanguageName.CPP,
    # Depended on this PR: https://github.com/tree-sitter/tree-sitter-cpp/pull/173
    # "ccm": LanguageName.CPP,
    # "c++m": LanguageName.CPP,
    # "cppm": LanguageName.CPP,
    # "cxxm": LanguageName.CPP,
    "cs": LanguageName.C_SHARP,
    "c": LanguageName.C,
    "h": LanguageName.C,
    "css": LanguageName.CSS,
    "php": LanguageName.PHP,
    "phtml": LanguageName.PHP,
    "php3": LanguageName.PHP,
    "php4": LanguageName.PHP,
    "php5": LanguageName.PHP,
    "php7": LanguageName.PHP,
    "phps": LanguageName.PHP,
    "php-s": LanguageName.PHP,
    "bash": LanguageName.BASH,
    "sh": LanguageName.BASH,
    "json": LanguageName.JSON,
    "ts": LanguageName.TYPESCRIPT,
    "mts": LanguageName.TYPESCRIPT,
    "cts": LanguageName.TYPESCRIPT,
    "tsx": LanguageName.TSX,
    # "vue": LanguageName.VUE,  # tree-sitter-vue parser is broken
    # The .wasm file being used is faulty, and yaml is split line-by-line anyway for the most part
    # "yaml": LanguageName.YAML,
    # "yml": LanguageName.YAML,
    "elm": LanguageName.ELM,
    "js": LanguageName.JAVASCRIPT,
    "jsx": LanguageName.JAVASCRIPT,
    "mjs": LanguageName.JAVASCRIPT,
    "cjs": LanguageName.JAVASCRIPT,
    "py": LanguageName.PYTHON,
    # "ipynb": LanguageName.PYTHON, # It contains Python, but the file format is a ton of JSON.
    "pyw": LanguageName.PYTHON,
    "pyi": LanguageName.PYTHON,
    "el": LanguageName.ELISP,
    "emacs": LanguageName.ELISP,
    "ex": LanguageName.ELIXIR,
    "exs": LanguageName.ELIXIR,
    "go": LanguageName.GO,
    "eex": LanguageName.EMBEDDED_TEMPLATE,
    "heex": LanguageName.EMBEDDED_TEMPLATE,
    "leex": LanguageName.EMBEDDED_TEMPLATE,
    "html": LanguageName.HTML,
    "htm": LanguageName.HTML,
    "java": LanguageName.JAVA,
    "lua": LanguageName.LUA,
    "luau": LanguageName.LUA,
    "ocaml": LanguageName.OCAML,
    "ml": LanguageName.OCAML,
    "mli": LanguageName.OCAML,
    "ql": LanguageName.QL,
    "res": LanguageName.RESCRIPT,
    "resi": LanguageName.RESCRIPT,
    "rb": LanguageName.RUBY,
    "erb": LanguageName.RUBY,
    "rs": LanguageName.RUST,
    "rdl": LanguageName.SYSTEMRDL,
    "toml": LanguageName.TOML,
    "sol": LanguageName.SOLIDITY,
    # "jl": LanguageName.JULIA,
    # "swift": LanguageName.SWIFT,
    # "kt": LanguageName.KOTLIN,
    # "scala": LanguageName.SCALA,
}


IGNORE_PATH_PATTERNS: Dict[LanguageName, List[re.Pattern]] = {
    LanguageName.TYPESCRIPT: [re.compile(r".*node_modules")],
    LanguageName.JAVASCRIPT: [re.compile(r".*node_modules")],
}


# ---------------------------------------------------------------------------
# Parser / language loading
# ---------------------------------------------------------------------------
_parser_initialized = False


async def _ensure_parser_init() -> None:
    global _parser_initialized
    if not _parser_initialized:
        _parser_initialized = True


async def get_parser_for_file(filepath: str) -> Optional[Parser]:
    try:
        await _ensure_parser_init()

        language = await get_language_for_file(filepath)
        if not language:
            return None

        # In modern tree_sitter Python bindings the language is passed to the
        # Parser constructor. Equivalent to parser.setLanguage(language).
        
        parser = Parser()
        parser.language = language
        return parser
    except Exception as e: 
        print(f"Unable to load language for file {filepath} {e}")
        return None


# Loading the wasm files to create a Language object is an expensive operation and with
# sufficient number of files can result in errors, instead keep a map of language name
# to Language object
name_to_language: Dict[LanguageName, Language] = {}


async def get_language_for_file(filepath: str) -> Optional[Language]:
    try:
        await _ensure_parser_init()

        extension = get_uri_file_extension(filepath)

        language_name = supported_languages.get(extension)
        if not language_name:
            return None

        language = name_to_language.get(language_name)
        if language is None:
            language = await load_language_for_file_ext(extension)
            name_to_language[language_name] = language
        return language
    except Exception as e:  # noqa: BLE001
        print(f"Unable to load language for file {filepath} {e}")
        return None


def get_full_language_name(filepath: str) -> Optional[LanguageName]:
    extension = get_uri_file_extension(filepath)
    return supported_languages.get(extension)


async def get_query_for_file(
    filepath: str,
    query_path: Path,
) -> Optional[Query]:
    language = await get_language_for_file(filepath)
    if not language:
        return None

    if not os.path.exists(query_path):
        return None

    with open(query_path, "r", encoding="utf-8") as fh:
        query_source = fh.read()

    query = Query(language, query_source)
    return query


async def load_language_for_file_ext(file_extension: str) -> Language:

    lang_name = supported_languages[file_extension]
    # `LanguageName` is a `str` enum so the raw string value is the grammar name.
    return _get_language_by_name(cast(Any, lang_name.value))


# See https://tree-sitter.github.io/tree-sitter/using-parsers
GET_SYMBOLS_FOR_NODE_TYPES: List[str] = [
    "class_declaration",
    "class_definition",
    "function_item",  # function name = first "identifier" child
    "function_definition",
    "method_declaration",  # method name = first "identifier" child
    "method_definition",
    "generator_function_declaration",
    # property_identifier
    # field_declaration
    # "arrow_function",
]


async def get_symbols_for_file(
    filepath: str,
    contents: str,
) -> Optional[List[SymbolWithRange]]:
    parser = await get_parser_for_file(filepath)
    if not parser:
        return None

    try:
        # tree_sitter requires bytes input.
        tree = parser.parse(contents.encode("utf-8"))
    except Exception:  # noqa: BLE001
        print(f"Error parsing file: {filepath}")
        return None

    symbols: List[SymbolWithRange] = []

    # Function to recursively find all named nodes (classes and functions)
    def find_named_nodes_recursive(node: Node) -> None:
        if node.type in GET_SYMBOLS_FOR_NODE_TYPES:
            # the actual name is the last identifier in the node
            # Especially with languages where return type is declared before the name
            identifier: Optional[Node] = None
            children = node.children
            for i in range(len(children) - 1, -1, -1):
                child_type = children[i].type
                if child_type == "identifier" or child_type == "property_identifier":
                    identifier = children[i]
                    break

            identifier_text = identifier.text if identifier is not None else None
            node_text = node.text
            if identifier_text is not None and node_text is not None:
                symbols.append(
                    SymbolWithRange(
                        filepath=filepath,
                        type=node.type,
                        name=identifier_text.decode("utf-8"),
                        range=Range(
                            start=RangePosition(
                                character=node.start_point.column,
                                line=node.start_point.row,
                            ),
                            end=RangePosition(
                                character=node.end_point.column,
                                line=node.end_point.row,
                            ),
                        ),
                        content=node_text.decode("utf-8"),
                    )
                )

        for child in node.children:
            find_named_nodes_recursive(child)

    find_named_nodes_recursive(tree.root_node)
    return symbols


async def get_symbols_for_many_files(
    uris: List[str],
    reader : FileSystem,
) -> FileSymbolMap:
    async def process(uri: str) -> tuple[str, List[SymbolWithRange]]:
        contents = await reader.read_file(uri)
        symbols: Optional[List[SymbolWithRange]] = None
        try:
            symbols = await get_symbols_for_file(uri, contents)
        except Exception as e:  # noqa: BLE001
            print(f"Failed to get symbols for {uri}: {e}")
        return (uri, symbols or [])

    results = await asyncio.gather(*(process(uri) for uri in uris))
    return dict(results)
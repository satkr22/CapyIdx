from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncGenerator, Literal, Optional, Union
from uuid import UUID, uuid4

from tree_sitter import Node

from base.index_d import ChunkingResult, Symbol, Chonk
from utils.count_tokens import count_tokens_async
from utils.tree_sitter import get_parser_for_file


# =============================================================================
# Helpers
# =============================================================================

def _node_text_bytes(node: Node) -> bytes:
    t = node.text
    if isinstance(t, str):
        return t.encode("utf-8")
    return t if t is not None else b""


def collapsed_replacement(node: Node) -> bytes:
    if node.type == "statement_block":
        return b"{ ... }"
    return b"..."


def first_child(node: Node, grammar_name: Union[str, list]) -> Optional[Node]:
    if isinstance(grammar_name, list):
        for child in node.children:
            if child.type in grammar_name:
                return child
        return None

    for child in node.children:
        if child.type == grammar_name:
            return child

    return None


def _extract_signature(content: str) -> str:
    """First line of the chunk content, trimmed. Used as a lightweight signature."""
    return content.split("\n", 1)[0].strip()


# =============================================================================
# Node Types
# =============================================================================

FUNCTION_BLOCK_NODE_TYPES = ["block", "statement_block"]

FUNCTION_DECLARATION_NODE_TYPES = [
    "method_definition",
    "function_definition",
    "function_item",
    "function_declaration",
    "method_declaration",
]

CLASS_NODE_TYPES = ("class_definition", "class_declaration", "impl_item")


# =============================================================================
# AST Helpers
# =============================================================================

def get_method_nodes(class_node: Node) -> list[Node]:
    block = first_child(
        class_node,
        ["block", "class_body", "declaration_list"],
    )

    if block is None:
        return []

    return [
        child
        for child in block.children
        if child.type in FUNCTION_DECLARATION_NODE_TYPES
    ]


# =============================================================================
# Class collapsing (overview chunk only)
# =============================================================================

async def collapse_children(
    node: Node,
    code: bytes,
    block_types: list,
    collapse_types: list,
    collapse_block_types: list,
    max_chunk_size: int,
) -> bytes:
    class_start = node.start_byte
    class_end = node.end_byte
    class_code = code[class_start:class_end]

    block = first_child(node, block_types)
    if block is None:
        return class_code

    parts: list[bytes] = []
    method_part_indices: list[int] = []

    last_pos = class_start
    for child in block.children:
        if child.type in collapse_types:
            grand_child = first_child(child, collapse_block_types)
            if grand_child is None:
                continue

            method_sig = code[child.start_byte:grand_child.start_byte]
            repl = collapsed_replacement(grand_child)
            method_part = method_sig + repl

            if child.start_byte > last_pos:
                parts.append(code[last_pos:child.start_byte])
            method_part_indices.append(len(parts))
            parts.append(method_part)
            last_pos = grand_child.end_byte

    if last_pos < class_end:
        parts.append(code[last_pos:class_end])

    collapsed = b"".join(parts)
    tokens = await count_tokens_async(collapsed.decode())

    # Too large: drop collapsed methods from the end until it fits.
    while method_part_indices and tokens > max_chunk_size:
        idx = method_part_indices.pop()
        parts.pop(idx)
        collapsed = b"".join(parts)
        tokens = await count_tokens_async(collapsed.decode())

    return collapsed


async def construct_class_definition_chunk(
    node: Node,
    code: bytes,
    max_chunk_size: int,
) -> bytes:
    return await collapse_children(
        node,
        code,
        ["block", "class_body", "declaration_list"],
        FUNCTION_DECLARATION_NODE_TYPES,
        FUNCTION_BLOCK_NODE_TYPES,
        max_chunk_size,
    )


# =============================================================================
# Large Function Splitting
# =============================================================================

def _build_function_header(
    node: Node,
    code: bytes,
) -> bytes:
    body_node = node.children[-1]
    signature = code[node.start_byte : body_node.start_byte]

    parent = node.parent
    class_node = parent.parent if parent else None

    is_in_class = (
        parent is not None
        and parent.type in ("block", "declaration_list")
        and class_node is not None
        and class_node.type in ("class_definition", "impl_item")
    )

    if is_in_class:
        assert parent is not None
        assert class_node is not None
        class_header = code[class_node.start_byte : parent.start_byte]
        indent = b" " * node.start_point[1]
        return class_header + b"...\n\n" + indent + signature

    return signature


async def _emit_body_chunks(
    header: bytes,
    statements: list[Node],
    code: bytes,
    max_chunk_size: int,
    func_start_line: int,
) -> AsyncGenerator[tuple[str, int, int], None]:
    header_tokens = await count_tokens_async(header.decode())
    budget = max(max_chunk_size - header_tokens, 1)

    i = 0
    while i < len(statements):
        start = i
        end = i

        while end + 1 < len(statements):
            span = code[
                statements[start].start_byte :
                statements[end + 1].end_byte
            ]
            if await count_tokens_async(span.decode()) > budget:
                break
            end += 1

        span = code[
            statements[start].start_byte :
            statements[end].end_byte
        ]
        combined = header + b"\n" + span

        # Every piece reports the *function's* start line.
        yield (
            combined.decode(),
            func_start_line,
            statements[end].end_point[0] + 1,
        )
        i = end + 1


async def split_large_function(
    node: Node,
    code: bytes,
    max_chunk_size: int,
) -> list[tuple[str, int, int]]:
    body = first_child(node, FUNCTION_BLOCK_NODE_TYPES)
    statements = [c for c in body.children if c.is_named] if body else []

    if not statements:
        return [
            (
                _node_text_bytes(node).decode(),
                node.start_point[0] + 1,
                node.end_point[0] + 1,
            )
        ]

    header = _build_function_header(node, code)
    func_start_line = node.start_point[0] + 1

    pieces: list[tuple[str, int, int]] = []
    async for p in _emit_body_chunks(
        header, statements, code, max_chunk_size, func_start_line
    ):
        pieces.append(p)
    return pieces


# =============================================================================
# Symbol registration helpers
# =============================================================================

def find_symbol(result: ChunkingResult, symbol_id: UUID) -> Symbol:
    return result.symbol_map[symbol_id]


def _register_symbol(result: ChunkingResult, symbol: Symbol) -> None:
    """Append symbol to graph AND attach it to its parent's children list."""
    result.symbols.append(symbol)
    result.symbol_map[symbol.id] = symbol

    if symbol.parent_id is not None:
        parent = result.symbol_map[symbol.parent_id]
        if symbol.id not in parent.children:
            parent.children.append(symbol.id)


def _make_chonk(
    symbol: Symbol,
    content: str,
    start_line: int,
    end_line: int,
    piece_index: int = 0,
    piece_count: int = 1,
    prev_chunk: UUID | None = None,
    next_chunk: UUID | None = None,
    chunk_id: UUID | None = None,
) -> Chonk:
    cid = chunk_id if chunk_id is not None else uuid4()
    symbol.chunk_ids.append(cid)
    return Chonk(
        id=cid,
        symbol_id=symbol.id,
        piece_index=piece_index,
        piece_count=piece_count,
        prev_chunk=prev_chunk,
        next_chunk=next_chunk,
        content=content,
        start_line=start_line,
        end_line=end_line,
        signature=_extract_signature(content),
    )


# =============================================================================
# Repository Intelligence Walker
# =============================================================================
# Async generator yielding Chonk as chunks are produced.
# Symbol graph (with children + chunk_ids) is built into `result`.

async def walk(
    node: Node,
    code: bytes,
    result: ChunkingResult,
    max_chunk_size: int,
    current_symbol_id: UUID,
    allow_chunking: bool = True,
) -> AsyncGenerator[Chonk, None]:

    # ---------------------------------------------------------------------
    # CLASS
    # ---------------------------------------------------------------------
    if node.type in CLASS_NODE_TYPES:
        name_node = first_child(node, "identifier")

        class_symbol = Symbol(
            id=uuid4(),
            type="class",
            name=name_node.text.decode()
            if name_node and name_node.text
            else "anonymous",
            parent_id=current_symbol_id,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
        _register_symbol(result, class_symbol)

        text = _node_text_bytes(node).decode()
        tokens = await count_tokens_async(text)

        block = first_child(node, ["block", "class_body", "declaration_list"])

        # -----------------------------------------------------------------
        # Small class → one class chunk only; methods get symbols only.
        # -----------------------------------------------------------------
        if tokens <= max_chunk_size:
            chunk = _make_chonk(
                class_symbol,
                content=text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
            result.chunks.append(chunk)
            yield chunk

            if block:
                for child in block.children:
                    async for nested in walk(
                        child,
                        code,
                        result,
                        max_chunk_size,
                        class_symbol.id,
                        allow_chunking=True, # allowed small methods too to get their own chunks
                    ):
                        yield nested
            return

        # -----------------------------------------------------------------
        # Large class → overview chunk + recursed method chunks.
        # -----------------------------------------------------------------
        overview = await construct_class_definition_chunk(
            node, code, max_chunk_size
        )
        chunk = _make_chonk(
            class_symbol,
            content=overview.decode(),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
        result.chunks.append(chunk)
        yield chunk

        if block:
            for child in block.children:
                async for nested in walk(
                    child,
                    code,
                    result,
                    max_chunk_size,
                    class_symbol.id,
                    allow_chunking=True,
                ):
                    yield nested
        return

    # ---------------------------------------------------------------------
    # METHOD / FUNCTION (every one gets its own Symbol)
    # ---------------------------------------------------------------------
    if node.type in FUNCTION_DECLARATION_NODE_TYPES:
        name_node = first_child(node, "identifier")

        func_symbol = Symbol(
            id=uuid4(),
            type="method",
            name=name_node.text.decode()
            if name_node and name_node.text
            else "anonymous",
            parent_id=current_symbol_id,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
        _register_symbol(result, func_symbol)

        if allow_chunking:
            full_text = _node_text_bytes(node).decode()
            tokens = await count_tokens_async(full_text)

            if tokens <= max_chunk_size:
                chunk = _make_chonk(
                    func_symbol,
                    content=full_text,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
                result.chunks.append(chunk)
                yield chunk
            else:
                pieces = await split_large_function(
                    node, code, max_chunk_size
                )
                chunk_ids = [uuid4() for _ in pieces]

                for i, (content, start, end) in enumerate(pieces):
                    chunk = _make_chonk(
                        func_symbol,
                        content=content,
                        start_line=start,
                        end_line=end,
                        piece_index=i,
                        piece_count=len(pieces),
                        prev_chunk=chunk_ids[i - 1] if i else None,
                        next_chunk=chunk_ids[i + 1]
                        if i < len(chunk_ids) - 1
                        else None,
                        chunk_id=chunk_ids[i],
                    )
                    result.chunks.append(chunk)
                    yield chunk

        # Recurse into the body for nested functions/classes (symbols only).
        body = first_child(node, FUNCTION_BLOCK_NODE_TYPES)
        if body:
            for child in body.children:
                async for nested in walk(
                    child,
                    code,
                    result,
                    max_chunk_size,
                    func_symbol.id,
                    allow_chunking=False,
                ):
                    yield nested
        return

    # ---------------------------------------------------------------------
    # Other nodes → DFS
    # ---------------------------------------------------------------------
    for child in node.children:
        async for nested in walk(
            child,
            code,
            result,
            max_chunk_size,
            current_symbol_id,
            allow_chunking,
        ):
            yield nested


# =============================================================================
# Entry Point
# =============================================================================
# Yields Chonk (with every field populated, incl. signature).
# Populates `result` with the full symbol graph (children + chunk_ids).

async def code_chunker(
    filepath: str,
    contents: str,
    max_chunk_size: int,
    result: ChunkingResult | None = None,
) -> AsyncGenerator[Chonk, None]:

    if len(contents.strip()) == 0:
        return

    parser = await get_parser_for_file(filepath)
    if parser is None:
        raise ValueError(f"Failed to load parser for {filepath}")

    code = contents.encode("utf-8")
    tree = parser.parse(code)

    if result is None:
        result = ChunkingResult()

    file_symbol = Symbol(
        id=uuid4(),
        type="file",
        name=filepath,
        parent_id=None,
        start_line=1,
        end_line=contents.count("\n") + 1,
    )
    _register_symbol(result, file_symbol)

    async for chunk in walk(
        tree.root_node,
        code,
        result,
        max_chunk_size,
        file_symbol.id,
        allow_chunking=True,
    ):
        yield chunk
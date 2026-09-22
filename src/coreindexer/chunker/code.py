from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncGenerator, Literal, Optional, Union
from uuid import UUID, uuid4
import logging

from tree_sitter import Node

from coreindexer.base.index_d import ChunkingResult, Symbol, Chonk
from coreindexer.utils.count_tokens import count_tokens_async
from coreindexer.utils.tree_sitter import get_parser_for_file


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

def _ast_signature(node: Node, code: bytes) -> str:
    if node.type in FUNCTION_DECLARATION_NODE_TYPES:
        body = first_child(node, FUNCTION_BLOCK_NODE_TYPES)
        if body is None:
            end = node.end_byte
        else:
            # Take the last child *before* the body; usually ':'.
            prev = None
            for child in node.children:
                if child.id == body.id:
                    break
                prev = child
            end = prev.end_byte if prev is not None else body.start_byte
        return code[node.start_byte:end].rstrip().decode()

    if node.type in CLASS_NODE_TYPES:
        block = first_child(node, ["block", "class_body", "declaration_list"])
        if block is None:
            end = node.end_byte
        else:
            prev = None
            for child in node.children:
                if child.id == block.id:
                    break
                prev = child
            end = prev.end_byte if prev is not None else block.start_byte
        return code[node.start_byte:end].rstrip().decode()

    return code[node.start_byte:node.end_byte].split(b"\n", 1)[0].strip().decode()

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

async def _char_split(blob: bytes, max_chunk_size: int) -> list[bytes]:
    
    # add logger for char spit
    print("Charater splitting triggered because any single symbol is excedding the max token bugdet.")
    
    logging.getLogger(__name__).warning(
        "char-split: %d bytes, first 200 chars: %r",
        len(blob), blob[:200].decode(errors="replace"),
    )
    
    text = blob.decode()
    out: list[bytes] = []
    i, n = 0, len(text)
    while i < n:
        lo, hi = i + 1, n
        best = i + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if await count_tokens_async(text[i:mid]) <= max_chunk_size:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        out.append(text[i:best].encode())
        i = best
    return out


async def _split_oversized(
    content: bytes,
    max_chunk_size: int,
) -> list[bytes]:
    """Final-resort splitter: cut a byte blob into <=budget pieces.

    Used when AST-aware splitting can't make a piece fit — class overviews
    after collapsing methods, or a single statement whose text alone exceeds
    budget. Splits at line boundaries; falls back to character boundaries
    for any single line that still doesn't fit.
    """
    if await count_tokens_async(content.decode()) <= max_chunk_size:
        return [content]

    pieces: list[bytes] = []
    current: list[bytes] = []
    
    current_tokens = 0
    
    for line in content.split(b"\n"):
        line_tokens = await count_tokens_async(line.decode())
        if current and current_tokens + line_tokens + 1 > max_chunk_size:
            pieces.append(b"\n".join(current))
            current = [line]
            current_tokens = line_tokens
        else:
            current.append(line)
            current_tokens += line_tokens + (1 if len(current) > 1 else 0)

    if current:
        pieces.append(b"\n".join(current))

    # Any single line that alone exceeds budget: char-split it.
    final: list[bytes] = []
    for p in pieces:
        if await count_tokens_async(p.decode()) <= max_chunk_size:
            final.append(p)
        else:
            final.extend(await _char_split(p, max_chunk_size))
    return final

def _collect_units(
    block: Node,
    code: bytes,
    collapse_types: list,
    collapse_block_types: list,
) -> list[bytes]:
    """Flatten a class body into units: text runs and collapsed declarations.

    Recurses into nested classes so their methods are also collapsed. Each
    unit is small (a method sig + '...' or a run of whitespace/comments), so
    packing units into pieces keeps every piece well under budget without
    ever needing byte-level splitting.
    """
    units: list[bytes] = []
    last_pos = block.start_byte
    for child in block.children:
        if child.type in collapse_types:
            grand_child = first_child(child, collapse_block_types)
            if grand_child is None:
                continue
            sig = code[child.start_byte:grand_child.start_byte]
            repl = collapsed_replacement(grand_child)
            if child.start_byte > last_pos:
                units.append(code[last_pos:child.start_byte])
            units.append(sig + repl)
            last_pos = grand_child.end_byte
        elif child.type in CLASS_NODE_TYPES:
            nested_block = first_child(
                child, ["block", "class_body", "declaration_list"]
            )
            if nested_block is None:
                continue
            if child.start_byte > last_pos:
                units.append(code[last_pos:child.start_byte])
            # Nested class header
            units.append(code[child.start_byte:nested_block.start_byte])
            # Recurse: collapse the nested class's methods too
            units.extend(_collect_units(
                nested_block, code, collapse_types, collapse_block_types
            ))
            # Nested class tail 
            if nested_block.end_byte < child.end_byte:
                units.append(code[nested_block.end_byte:child.end_byte])
            last_pos = child.end_byte
    if last_pos < block.end_byte:
        units.append(code[last_pos:block.end_byte])
    return units





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
) -> list[bytes]:
    """Collapse a class body into one or more overview pieces.

    Each returned piece fits under `max_chunk_size`. The class header is
    repeated at the top of every piece so each reads as a standalone
    overview. Pieces are packed at unit boundaries, so no piece ever
    breaks mid-token; the byte-level splitter is only touched if a single
    unit is somehow still over budget.
    """
    class_start = node.start_byte
    class_end = node.end_byte

    block = first_child(node, block_types)
    if block is None:
        return [code[class_start:class_end]]

    header = code[class_start:block.start_byte]  # "class Foo:" + newline

    units = _collect_units(block, code, collapse_types, collapse_block_types)

    # Special case: body collapsed to nothing but the header and tail.
    if not units:
        tail = code[block.end_byte:class_end]
        return [header + tail]

    header_tokens = await count_tokens_async(header.decode())
    budget = max(max_chunk_size - header_tokens, 1)

    pieces: list[bytes] = []
    current: list[bytes] = []
    current_tokens = 0

    for unit in units:
        unit_tokens = await count_tokens_async(unit.decode())

        # Defensive: a single unit over budget. Should be impossible after
        # collapsing (sig + "..." is tiny), except for pathological cases
        # like a giant class docstring. Split it at line boundaries.
        if unit_tokens > budget:
            if current:
                pieces.append(header + b"".join(current))
                current, current_tokens = [], 0
            for sub in await _split_oversized(unit, budget):
                pieces.append(header + sub)
            continue

        if current and current_tokens + unit_tokens > budget:
            pieces.append(header + b"".join(current))
            current, current_tokens = [], 0

        current.append(unit)
        current_tokens += unit_tokens

    if current:
        pieces.append(header + b"".join(current))

    return pieces or [header]




async def construct_class_definition_chunk(
    node: Node,
    code: bytes,
    max_chunk_size: int,
) -> list[bytes]:
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
        running = await count_tokens_async(
            code[statements[start].start_byte : statements[start].end_byte].decode()
        )

        while end + 1 < len(statements):
            nxt = await count_tokens_async(
                code[statements[end + 1].start_byte : statements[end + 1].end_byte].decode()
            )
            # +1 for the newline separator between statements
            if running + nxt + 1 > budget:
                break
            running += nxt + 1
            end += 1

        span = code[statements[start].start_byte : statements[end].end_byte]
        combined = header + b"\n" + span
        end_line = statements[end].end_point[0] + 1

        # Exact check of the actually-emitted string:
        if await count_tokens_async(combined.decode()) <= max_chunk_size:
            yield (combined.decode(), func_start_line, end_line)
        else:
            for piece in await _split_oversized(combined, max_chunk_size):
                yield (piece.decode(), func_start_line, end_line)
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
        # if symbol.id not in parent.children:
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
    signature: Optional[str] = None
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
        signature=signature if signature is not None else _extract_signature(content),
    )


# =============================================================================
# Repository Intelligence Walker
# =============================================================================
# Async generator yielding Chonk as chunks are produced.
# Symbol graph (with children + chunk_ids) is built into result.

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
        sig = _ast_signature(node, code)
        
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
                signature=sig
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
        overview_pieces = await         construct_class_definition_chunk(
            node, code, max_chunk_size
        )

        class_start = node.start_point[0] + 1
        class_end = node.end_point[0] + 1
        chunk_ids = [uuid4() for _ in overview_pieces]

        for i, piece in enumerate(overview_pieces):
            chunk = _make_chonk(
                class_symbol,
                content=piece.decode(),
                start_line=class_start,
                end_line=class_end,
                piece_index=i,
                piece_count=len(overview_pieces),
                prev_chunk=chunk_ids[i - 1] if i else None,
                next_chunk=chunk_ids[i + 1]
                if i < len(chunk_ids) - 1
                else None,
                chunk_id=chunk_ids[i],
                signature=sig,
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
        sig = _ast_signature(node, code)
        
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
                    signature=sig
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
                        signature=sig
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
                    allow_chunking=True, # allow nested functions to get their own chunks
                ):
                    yield nested
        return

    # ---------------------------------------------------------------------
    # Other nodes - DFS
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
# Yields Chonk (with every field populated including signature)
# Populates result with the full symbol graph (children + chunk_ids).

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
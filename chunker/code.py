"""
Python port of code.ts

Preserves the behaviour of the original TypeScript code chunker.

Notes on the port:
- `node.startIndex` / `node.endIndex` (web-tree-sitter, JS string offsets) map to
  `node.start_byte` / `node.end_byte` (tree_sitter Python, UTF-8 byte offsets).
  To keep slicing consistent we work with `bytes` throughout and decode to `str`
  only when producing chunk content.
- `node.startPosition.row/column` maps to `node.start_point[0]` / `node.start_point[1]`.
- `node.endPosition.row` maps to `node.end_point[0]`.
- `node.text` is `bytes` in the tree_sitter Python bindings.
"""

from typing import AsyncGenerator, Optional, Union
from uuid import UUID, uuid4
from tree_sitter import Node

from utils.count_tokens import count_tokens_async
from utils.tree_sitter import get_parser_for_file
from base.index_d import ChunkWithoutID



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_text_bytes(node: Node) -> bytes:
    """Return node.text as bytes (tree_sitter Python gives bytes; be tolerant)."""
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


# ---------------------------------------------------------------------------
# collapseChildren
# ---------------------------------------------------------------------------

async def collapse_children(
    node: Node,
    code: bytes,
    block_types: list,
    collapse_types: list,
    collapse_block_types: list,
    max_chunk_size: int,
) -> bytes:
    
    code = code[: node.end_byte]
    block = first_child(node, block_types)
    collapsed_children: list[bytes] = []
    method_symbols = []
    if block is not None:
        children_to_collapse = [
            child for child in block.children if child.type in collapse_types
        ]
        for child in reversed(children_to_collapse):
            
            grand_child = first_child(child, collapse_block_types)
            if grand_child is not None:
                start = grand_child.start_byte
                end = grand_child.end_byte
                repl = collapsed_replacement(grand_child)
                collapsed_child = code[child.start_byte:start] + repl
                code = code[:start] + repl + code[end:]
                collapsed_children.insert(0, collapsed_child)

    code = code[node.start_byte:]
    removed_child = False
    while (
        (await count_tokens_async(code.strip().decode("utf-8"))) > max_chunk_size
        and collapsed_children
    ):
        removed_child = True
        # Remove children starting at the end - TODO: Add multiple chunks so no children are missing
        child_code = collapsed_children.pop()
        index = code.rfind(child_code)
        if index > 0:
            code = code[:index] + code[index + len(child_code):]

    if removed_child:
        # Remove the extra blank lines
        lines = code.split(b"\n")
        first_white_space_in_group = -1
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == b"":
                if first_white_space_in_group < 0:
                    first_white_space_in_group = i
            else:
                if first_white_space_in_group - i > 1:
                    # Remove the lines
                    lines = lines[: i + 1] + lines[first_white_space_in_group + 1:]
                first_white_space_in_group = -1

        code = b"\n".join(lines)

    return code


# ---------------------------------------------------------------------------
# Node type constants
# ---------------------------------------------------------------------------

FUNCTION_BLOCK_NODE_TYPES = ["block", "statement_block"]
FUNCTION_DECLARATION_NODE_TYPES = [
    "method_definition",
    "function_definition",
    "function_item",
    "function_declaration",
    "method_declaration",
]


# ---------------------------------------------------------------------------
# Chunk constructors
# ---------------------------------------------------------------------------

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


async def construct_function_definition_chunk(
    node: Node,
    code: bytes,
    max_chunk_size: int,
) -> bytes:
    body_node = node.children[-1]
    collapsed_body = collapsed_replacement(body_node)
    signature = code[node.start_byte:body_node.start_byte]
    func_text = signature + collapsed_body

    parent_node = node.parent
    class_node = parent_node.parent if parent_node is not None else None
    is_in_class = (
        parent_node is not None
        and parent_node.type in ("block", "declaration_list")
        and class_node is not None
        and class_node.type in ("class_definition", "impl_item")
    )

    if is_in_class:
        assert parent_node is not None
        assert class_node is not None
        class_block = parent_node
        class_header = code[class_node.start_byte:class_block.start_byte]
        indent = b" " * node.start_point[1] # start_point[1] == leading-whitespace count because indentation is always ASCII
        combined = class_header + b"...\n\n" + indent + func_text

        if (await count_tokens_async(combined.decode("utf-8"))) <= max_chunk_size:
            return combined
        if (await count_tokens_async(func_text.decode("utf-8"))) <= max_chunk_size:
            return func_text
        first_line = signature.split(b"\n")[0]
        minimal = first_line + b" " + collapsed_body
        if (await count_tokens_async(minimal.decode("utf-8"))) <= max_chunk_size:
            return minimal
        return collapsed_body

    if (await count_tokens_async(func_text.decode("utf-8"))) <= max_chunk_size:
        return func_text
    first_line = signature.split(b"\n")[0]
    minimal = first_line + b" " + collapsed_body
    if (await count_tokens_async(minimal.decode("utf-8"))) <= max_chunk_size:
        return minimal
    return collapsed_body


# ---------------------------------------------------------------------------
# Splitting large functions/methods (preserve all code, never collapse it away)
# ---------------------------------------------------------------------------

def _build_function_header(node: Node, code: bytes) -> bytes:
    """
    Build the "context header" for a function/method: its own signature, plus
    (if it lives inside a class/impl block) the enclosing class signature.
    This mirrors the is_in_class branch of construct_function_definition_chunk,
    but returns only the header - no body/placeholder - since callers append
    their own body content after it.
    """
    body_node = node.children[-1]
    signature = code[node.start_byte:body_node.start_byte]

    parent_node = node.parent
    class_node = parent_node.parent if parent_node is not None else None
    is_in_class = (
        parent_node is not None
        and parent_node.type in ("block", "declaration_list")
        and class_node is not None
        and class_node.type in ("class_definition", "impl_item")
    )

    if is_in_class:
        assert parent_node is not None
        assert class_node is not None
        class_block = parent_node
        class_header = code[class_node.start_byte:class_block.start_byte]
        indent = b" " * node.start_point[1]
        return class_header + b"...\n\n" + indent + signature

    return signature


async def _emit_body_chunks(
    header: bytes,
    statements: list,
    code: bytes,
    max_chunk_size: int,
) -> AsyncGenerator[ChunkWithoutID, None]:
    """
    Greedily group consecutive `statements` into chunks so that each chunk,
    combined with `header`, stays within max_chunk_size tokens. Every emitted
    chunk therefore carries the full signature context (method + enclosing
    class, via `header`) even though it only holds a slice of the body.

    If a single statement doesn't fit even on its own (e.g. one huge `if`/`for`
    block), we recurse into its own nested block using its own header line, so
    we split further instead of dropping code. If there's nothing left to
    recurse into, we fall back to emitting it as one oversized chunk - staying
    under the token limit is secondary to never losing code.
    """
    header_tokens = await count_tokens_async(header.decode("utf-8"))
    budget = max(max_chunk_size - header_tokens, 1)

    i, n = 0, len(statements)
    while i < n:
        start = i
        end = i
        # Extend the group as far as possible while staying under budget.
        while end + 1 < n:
            span = code[statements[start].start_byte:statements[end + 1].end_byte]
            if (await count_tokens_async(span.decode("utf-8"))) > budget:
                break
            end += 1

        span = code[statements[start].start_byte:statements[end].end_byte]
        span_tokens = await count_tokens_async(span.decode("utf-8"))

        if start == end and span_tokens > budget:
            stmt = statements[start]
            inner_block = first_child(stmt, FUNCTION_BLOCK_NODE_TYPES)
            inner_statements = (
                [c for c in inner_block.children if c.is_named] if inner_block else []
            )
            if inner_statements:
                # e.g. a single statement that is itself a big `if`/`for`/`try`:
                # keep splitting, carrying the outer header plus this statement's
                # own signature line forward as the new header.
                nested_header = (
                    header + b"\n" + code[stmt.start_byte:inner_block.start_byte] + b"...\n" # type: ignore
                )
                async for c in _emit_body_chunks(
                    nested_header, inner_statements, code, max_chunk_size
                ):
                    yield c
            else:
                # Nothing left to split - emit as-is even if it exceeds the
                # limit. Preserving the code takes priority over the budget.
                combined = header + b"\n" + span
                yield ChunkWithoutID(
                    content=combined.decode("utf-8"),
                    start_line=stmt.start_point[0]+1,
                    end_line=stmt.end_point[0]+1,
                )
        else:
            combined = header + b"\n" + span
            yield ChunkWithoutID(
                content=combined.decode("utf-8"),
                start_line=statements[start].start_point[0]+1,
                end_line=statements[end].end_point[0]+1,
            )

        i = end + 1


async def split_large_function(
    node: Node,
    code: bytes,
    max_chunk_size: int,
) -> AsyncGenerator[ChunkWithoutID, None]:
    """
    Entry point for a function/method whose full text doesn't fit in one
    chunk. Instead of collapsing the body away (losing the code), split the
    body into consecutive chunks - each one prefixed with the method
    signature and, if applicable, the enclosing class signature - so the
    entire body is preserved across possibly-many chunks.
    """
    body_node = first_child(node, FUNCTION_BLOCK_NODE_TYPES)
    statements = [c for c in body_node.children if c.is_named] if body_node else []

    if not statements:
        # No body to split (e.g. an interface/abstract stub) - best effort.
        text = _node_text_bytes(node).decode("utf-8")
        yield ChunkWithoutID(
            content=text,
            start_line=node.start_point[0]+1,
            end_line=node.end_point[0]+1,
        )
        return

    header = _build_function_header(node, code)
    async for c in _emit_body_chunks(header, statements, code, max_chunk_size):
        yield c


collapsed_node_constructors = {
    # Classes, structs, etc
    "class_definition": construct_class_definition_chunk,
    "class_declaration": construct_class_definition_chunk,
    "impl_item": construct_class_definition_chunk,
    # Functions
    "function_definition": construct_function_definition_chunk,
    "function_declaration": construct_function_definition_chunk,
    "function_item": construct_function_definition_chunk,
    # Methods
    "method_declaration": construct_function_definition_chunk,
    # Properties
}


# ---------------------------------------------------------------------------
# Chunk emission
# --------------------------------------------------------------------------

async def maybe_yield_chunk(
    node: Node,
    code: bytes,
    max_chunk_size: int,
    root: bool = True,
) -> Optional[ChunkWithoutID]:
    # Keep entire text if not over size
    if root or node.type in collapsed_node_constructors:
        text = _node_text_bytes(node)
        token_count = await count_tokens_async(text.decode("utf-8"))
        if token_count < max_chunk_size:
            return ChunkWithoutID(
                content=text.decode("utf-8"),
                start_line=node.start_point[0]+1,
                end_line=node.end_point[0]+1,
            )
    return None


async def get_smart_collapsed_chunks(
    node: Node,
    code: bytes,
    max_chunk_size: int,
    root: bool = True
) -> AsyncGenerator[ChunkWithoutID, None]:
    
    # full content emit in chunk
    chunk = await maybe_yield_chunk(node, code, max_chunk_size, root)
    if chunk is not None:
        yield chunk
        return

    # A function/method that's too big to emit whole: split its body into
    # multiple chunks (each headed by its signature + enclosing class
    # signature) instead of collapsing it away, so no code is lost.
    if node.type in FUNCTION_DECLARATION_NODE_TYPES:
        async for c in split_large_function(node, code, max_chunk_size):
            yield c

    # Otherwise, if a collapsed form is defined (classes, structs, etc), use that
    elif node.type in collapsed_node_constructors:
        content_text = await collapsed_node_constructors[node.type](
            node, code, max_chunk_size
        )
        yield ChunkWithoutID(
            content=content_text.decode("utf-8"),
            start_line=node.start_point[0]+1,
            end_line=node.end_point[0]+1,
        )

    # Recurse (because even if collapsed version was shown, want to show the children in full somewhere)
    for child in node.children:
        async for c in get_smart_collapsed_chunks(child, code, max_chunk_size, False):
            yield c


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def code_chunker(
    filepath: str,
    contents: str,
    max_chunk_size: int,
) -> AsyncGenerator[ChunkWithoutID, None]:
    if len(contents.strip()) == 0:
        return

    parser = await get_parser_for_file(filepath)
    if parser is None:
        raise ValueError(f"Failed to load parser for file {filepath}: ")

    contents_bytes = contents.encode("utf-8")
    tree = parser.parse(contents_bytes)

    async for chunk in get_smart_collapsed_chunks(
        tree.root_node, contents_bytes, max_chunk_size
    ):
        yield chunk
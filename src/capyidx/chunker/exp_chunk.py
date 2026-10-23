import asyncio
import re
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, List, Optional, Tuple, cast
from uuid import uuid4

from tree_sitter import Parser, Query, QueryCursor, Language

from capyidx.base.index_d import Chunk, ChunkWithoutID, ChunkingResult, Chonk, Symbol
from capyidx.utils.count_tokens import count_tokens_async
from capyidx.utils.tree_sitter_utils import supported_languages
from capyidx.utils.uri1 import get_uri_file_extension, get_uri_path_basename
from capyidx.chunker.basic import basic_chunker
from capyidx.chunker.code import code_chunker 

try:
    from capyidx.utils.tree_sitter_queries.all_queries import LANGUAGE_QUERIES
except ImportError:
    LANGUAGE_QUERIES = {}

# ---------------------------------------------------------------------------
# Query-path configuration
# ---------------------------------------------------------------------------

EXTENSION_TO_QUERY_KEY = {
    "py": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript",
    "java": "java",
    "rs": "rust",
    "rb": "ruby",
    "cs": "c_sharp",
    "c": "c", "h": "c",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp",
    "el": "elisp",
    "go": "go",
    "ml": "ocaml",
    "php": "php",
    "ql": "ql",
}

INCLUDE_CONTAINER_SYMBOLS = True
_CONTAINER_TYPES = {"class", "interface", "module"}

_TYPE_MAP = {
    "class": "class",
    "method": "method",
    "function": "function",
    "decorator": "function",  
    "interface": "interface",
    "module": "module",
    "enum": "enum",
    "macro": "macro",
    "type": "type",
    "union": "type",
}

_QUERY_CACHE: Dict[str, Optional[Query]] = {}
_PARSER_CACHE: Dict[str, Parser] = {}

_CUSTOM_DIRECTIVE_RE = re.compile(r"\(\#(?:strip!|select-adjacent!|set-adjacent!)[^)]*\)")
_LOCALS_PRED_RE = re.compile(r"\(\#is-not\?\s+local\)")
_ANY_PRED_RE = re.compile(r"\(\#[A-Za-z0-9_-]+[!?][^()]*\)")

def _compile_query(extension: str) -> Optional[Query]:
    if extension in _QUERY_CACHE:
        return _QUERY_CACHE[extension]

    query_obj: Optional[Query] = None
    qkey = EXTENSION_TO_QUERY_KEY.get(extension)
    
    # Safely fetch language reference
    lang_supported = supported_languages.get(extension)
    lang: Optional[Language] = cast(Language, lang_supported.value) if lang_supported else None
    raw = LANGUAGE_QUERIES.get(qkey) if qkey else None

    if raw and lang is not None:
        sanitized = _CUSTOM_DIRECTIVE_RE.sub("", raw)
        sanitized = _LOCALS_PRED_RE.sub("", sanitized)
        bare = _ANY_PRED_RE.sub("", sanitized)
        for candidate in (sanitized, bare):
            try:
                query_obj = Query(lang, candidate) 
                break
            except Exception:
                continue

    _QUERY_CACHE[extension] = query_obj
    return query_obj

def _get_parser(extension: str) -> Parser:
    parser = _PARSER_CACHE.get(extension)
    if parser is None:
        lang_supported = supported_languages.get(extension)
        lang: Language = cast(Language, lang_supported.value) if lang_supported else None
        parser = Parser(language=lang)
        _PARSER_CACHE[extension] = parser
    return parser

@dataclass
class _SymRec:
    symbol: Symbol
    start_byte: int
    end_byte: int
    doc_text: str = ""

def _clean_doc(lines: List[str]) -> str:
    out = []
    for line in lines:
        s = line.strip()
        s = re.sub(r"^/\*\*?", "", s)
        s = re.sub(r"\*/$", "", s)
        s = re.sub(r"^(//+|;+|;{2,}|#+|\*+)\s?", "", s)
        out.append(s.rstrip())
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)

def _extract_symbol_records(
    root_node,
    source_bytes: bytes,
    query: Query,
) -> List[_SymRec]:
    cursor = QueryCursor(query)
    captures = cursor.captures(root_node)

    defs: List[Tuple[object, str]] = []
    names: List[Tuple[object, str]] = []
    docs = []
    for node, cname in captures:
        if cname.startswith("definition."):
            defs.append((node, cname.split(".", 1)[1]))
        elif cname.startswith("name.definition."):
            names.append((node, cname.split(".", 1)[1]))
        elif cname == "doc":
            docs.append(node)

    recs: List[_SymRec] = []
    for dnode, dtype in defs:
        sym_type = _TYPE_MAP.get(dtype)
        if sym_type is None:
            continue

        name_node = None
        for nnode, ntype in names:
            if ntype != dtype:
                continue
            if nnode.start_byte >= dnode.start_byte and nnode.end_byte <= dnode.end_byte:
                if name_node is None or (
                    nnode.end_byte - nnode.start_byte
                    < name_node.end_byte - name_node.start_byte
                ):
                    name_node = nnode

        if name_node is not None:
            name = name_node.text.decode("utf-8", errors="replace")
        else:
            fb = dnode.child_by_field_name("name")
            if fb is not None:
                name = fb.text.decode("utf-8", errors="replace")
            else:
                # Handle fallback naming cleanly for anonymous default exports
                name = "default" if "default" in dnode.type else f"symbol_{dnode.start_byte}"

        if dtype == "method" and name == "constructor":
            continue

        if not INCLUDE_CONTAINER_SYMBOLS and sym_type in _CONTAINER_TYPES:
            continue

        recs.append(
            _SymRec(
                symbol=Symbol(
                    id=uuid4(),
                    type=sym_type,
                    name=name,
                    parent_id=None,
                    start_line=dnode.start_point[0] + 1,
                    end_line=dnode.end_point[0] + 1,
                ),
                start_byte=dnode.start_byte,
                end_byte=dnode.end_byte,
            )
        )

    recs.sort(key=lambda r: (r.start_byte, -(r.end_byte - r.start_byte)))
    accepted: List[_SymRec] = []
    for r in recs:
        # FIX: Ensure we only deduplicate structural overlaps if they represent 
        # identical spatial definitions (same start) to prevent method-swallowing.
        dup = any(
            a.start_byte == r.start_byte
            and a.end_byte >= r.end_byte
            and (a.end_byte - a.start_byte) > (r.end_byte - r.start_byte)
            for a in accepted
        )
        if not dup:
            accepted.append(r)

    if docs:
        docs.sort(key=lambda n: n.start_byte)
        text = source_bytes.decode("utf-8", errors="replace")
        for r in accepted:
            taken = []
            last_end = r.start_byte
            for d in reversed(docs):
                if d.end_byte > last_end:
                    continue
                gap = text[d.end_byte:last_end]
                if gap.strip() == "":
                    taken.append(d.text.decode("utf-8", errors="replace"))
                    last_end = d.start_byte
                else:
                    break
            if taken:
                r.doc_text = _clean_doc(list(reversed(taken)))

    stack: List[_SymRec] = []
    for r in accepted:
        while stack and stack[-1].end_byte <= r.start_byte:
            stack.pop()
        if stack:
            r.symbol.parent_id = stack[-1].symbol.id
        stack.append(r)

    return accepted

async def _chunks_from_records(
    recs: List[_SymRec],
    file_uri: str,
    contents: str,
    source_bytes: bytes,
    max_chunk_size: int,
    result: ChunkingResult,
) -> AsyncGenerator[Chonk, None]:
    file_symbol = Symbol(
        id=uuid4(),
        type="file",
        name=file_uri,
        parent_id=None,
        start_line=1,
        end_line=contents.count("\n") + 1,
    )
    result.symbols.append(file_symbol)
    result.symbol_map[file_symbol.id] = file_symbol

    for r in recs:
        body = source_bytes[r.start_byte:r.end_byte].decode("utf-8", errors="replace")
        content = f"{r.doc_text}\n{body}" if r.doc_text else body

        pieces: List[dict] = []
        if len(content) > max_chunk_size * 4 and await count_tokens_async(content) > max_chunk_size:
            async for cd in basic_chunker(content, max_chunk_size):
                if await count_tokens_async(cd["content"]) > max_chunk_size:
                    continue
                pieces.append(cd)
        else:
            pieces.append(
                {
                    "content": content,
                    "startLine": r.symbol.start_line,
                    "endLine": r.symbol.end_line,
                }
            )

        chonks: List[Chonk] = []
        for i, cd in enumerate(pieces):
            if len(pieces) > 1:
                start_line = r.symbol.start_line + cd["startLine"] - 1
                end_line = r.symbol.start_line + cd["endLine"] - 1
            else:
                start_line, end_line = cd["startLine"], cd["endLine"]
            chonks.append(
                Chonk(
                    id=uuid4(),
                    symbol_id=r.symbol.id,
                    piece_index=i,
                    piece_count=len(pieces),
                    prev_chunk=None,
                    next_chunk=None,
                    content=cd["content"],
                    start_line=start_line,
                    end_line=end_line,
                    signature=cd["content"].split("\n", 1)[0].strip(),
                )
            )

        for i in range(len(chonks)):
            if i > 0:
                chonks[i].prev_chunk = chonks[i - 1].id
            if i < len(chonks) - 1:
                chonks[i].next_chunk = chonks[i + 1].id

        for ch in chonks:
            r.symbol.chunk_ids.append(ch.id)  # type: ignore
            result.chunks.append(ch)
            yield ch

async def _query_code_chunker(
    file_uri: str,
    contents: str,
    max_chunk_size: int,
    result: ChunkingResult,
) -> AsyncGenerator[Chonk, None]:
    extension = get_uri_file_extension(file_uri)
    query = _compile_query(extension)
    if query is None:
        return
    parser = _get_parser(extension)
    source_bytes = contents.encode("utf-8", errors="replace")
    tree = parser.parse(source_bytes)
    recs = _extract_symbol_records(tree.root_node, source_bytes, query)
    if not recs:
        return
    
    
    async for chonk in _chunks_from_records(
    recs, file_uri, contents, source_bytes, max_chunk_size, result
    ):
        yield chonk
            
@dataclass
class ChunkDocumentParam:
    filepath: str
    contents: str
    maxChunkSize: int
    digest: str
    NON_CODE_EXTENSIONS = ["css", "html", "htm", "json", "toml", "yaml", "yml"]
        
        
async def chunk_document_without_id(
    file_uri: str,
    contents: str,
    max_chunk_size: int,
    ) -> AsyncGenerator[Chonk | ChunkingResult, None]:
    if len(contents.strip()) == 0:
        return
    result = ChunkingResult()
    extension = get_uri_file_extension(file_uri)
    if extension in supported_languages and extension not in NON_CODE_EXTENSIONS:
        produced = False
        try:
            async for chunk in _query_code_chunker(
            file_uri, contents, max_chunk_size, result
            ):
                produced = True
                yield chunk
            if produced:
                    yield result
                    return
        except Exception:
            result = ChunkingResult()
        try:
            async for chunk in code_chunker(
            file_uri, contents, max_chunk_size, result
            ):
                yield chunk
            yield result
            return
        except Exception:
            result = ChunkingResult()
    file_symbol = Symbol(
        id=uuid4(),
        type="file",
        name=file_uri,
        parent_id=None,
        start_line=1,
        end_line=contents.count("\n") + 1,
    )
    result.symbols.append(file_symbol)
    result.symbol_map[file_symbol.id] = file_symbol
    idx = 0
    async for chunk_dict in basic_chunker(contents, max_chunk_size):
        content = chunk_dict["content"]
        if await count_tokens_async(content) > max_chunk_size:
            continue
        ch = Chonk(
            id=uuid4(),
            symbol_id=file_symbol.id,
            piece_index=idx,
            piece_count=1,
            prev_chunk=None,
            next_chunk=None,
            content=content,
            start_line=chunk_dict["startLine"],
            end_line=chunk_dict["endLine"],
            signature=content.split("\n", 1)[0].strip(),
        )
        file_symbol.chunk_ids.append(ch.id)  # type: ignore
        result.chunks.append(ch)
        idx += 1
        yield ch
    yield result


async def chunk_document(
    param: ChunkDocumentParam,
) -> AsyncGenerator[Chunk | ChunkingResult, None]:
    
    filepath = param.filepath
    contents = param.contents
    max_chunk_size = param.maxChunkSize
    digest = param.digest
    index_box: List[int] = [0]
    async def _resolve_chunk(chunk_without_id: Chonk) -> Optional[Chunk]:
        if await count_tokens_async(chunk_without_id.content) > max_chunk_size:
            return None
        current_index = index_box[0]
        index_box[0] += 1
        return Chunk(
            id=chunk_without_id.id,
            symbol_id=chunk_without_id.symbol_id,
            piece_index=chunk_without_id.piece_index,
            piece_count=chunk_without_id.piece_count,
            prev_chunk=chunk_without_id.prev_chunk,
            next_chunk=chunk_without_id.next_chunk,
            content=chunk_without_id.content,
            start_line=chunk_without_id.start_line,
            end_line=chunk_without_id.end_line,
            signature=chunk_without_id.signature,
            digest=digest,
            filepath=filepath,
            index=current_index,
        )
    
    chunk_tasks: List[asyncio.Task] = []
    async for chunk_without_id in chunk_document_without_id(
    filepath, contents, max_chunk_size
    ):
        if isinstance(chunk_without_id, ChunkingResult):
            yield chunk_without_id
        else:
            chunk_tasks.append(asyncio.create_task(_resolve_chunk(chunk_without_id)))
    for task in chunk_tasks:
        chunk = await task
        if chunk is None:
            continue
        yield chunk


def should_chunk(file_uri: str, contents: str) -> bool:
    if len(contents) > 1000000:
        return False
    if len(contents) == 0:
        return False
    base_name = get_uri_path_basename(file_uri)
    return "." in base_name
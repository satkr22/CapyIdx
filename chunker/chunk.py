import asyncio
from dataclasses import dataclass
from typing import AsyncGenerator, List, Optional
from uuid import uuid4

from base.index_d import Chunk, ChunkWithoutID, ChunkingResult, Chonk, Symbol
from utils.count_tokens import count_tokens_async
from utils.tree_sitter import supported_languages
from utils.uri import get_uri_file_extension, get_uri_path_basename
from chunker.basic import basic_chunker
from chunker.code import code_chunker


@dataclass
class ChunkDocumentParam:
    filepath: str
    contents: str
    maxChunkSize: int
    digest: str


# Extensions that should use basicChunker despite having tree-sitter support.
# These files don't have code structure (classes/functions) that codeChunker
# expects.
NON_CODE_EXTENSIONS = [
    "css",
    "html",
    "htm",
    "json",
    "toml",
    "yaml",
    "yml",
]


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
        try:
            async for chunk in code_chunker(
                file_uri, contents, max_chunk_size, result
            ):
                yield chunk
            yield result
            return
        except Exception:
            # reset — code_chunker may have half-populated result before raising
            result = ChunkingResult()

    # ---------------- Non-code fallback ----------------
    # Build a file symbol so every chunk has an owner and the DB FK holds.
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
        file_symbol.chunk_ids.append(ch.id) # type: ignore
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

    async def _resolve_chunk(
        chunk_without_id: Chonk,
    ) -> Optional[Chunk]:

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
            chunk_tasks.append(
                asyncio.create_task(_resolve_chunk(chunk_without_id))
            )

    for task in chunk_tasks:
        chunk = await task
        if chunk is None:
            # log
            print("dropping chunks because its size limit exceeds")
            continue
        yield chunk


def should_chunk(file_uri: str, contents: str) -> bool:
    if len(contents) > 1000000:
        # if a file has more than 1m characters then skip it
        return False
    if len(contents) == 0:
        return False
    base_name = get_uri_path_basename(file_uri)
    return "." in base_name
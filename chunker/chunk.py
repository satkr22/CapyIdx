"""
Python port of chunk.ts

Preserves the behaviour of the original TypeScript chunking module,
including:
  * concurrent token-count verification of every chunk
  * index assignment based on completion order (not yield order)
  * yielding chunks in the original chunker's order, skipping any that
    exceed maxChunkSize
"""

import asyncio
from dataclasses import dataclass
from typing import AsyncGenerator, List, Optional

from base.index_d import Chunk, ChunkWithoutID
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
) -> AsyncGenerator[ChunkWithoutID, None]:
    if len(contents.strip()) == 0:
        return

    extension = get_uri_file_extension(file_uri)

    if extension in supported_languages and extension not in NON_CODE_EXTENSIONS:
        try:
            async for chunk in code_chunker(file_uri, contents, max_chunk_size):
                yield chunk
            return
        except Exception:
            # fall back to basicChunker
            pass

    async for chunk_dict in basic_chunker(contents, max_chunk_size):
        yield ChunkWithoutID(
            content=chunk_dict["content"],
            start_line=chunk_dict["startLine"],
            end_line=chunk_dict["endLine"],
        )


async def chunk_document(
    param: ChunkDocumentParam,
) -> AsyncGenerator[Chunk, None]:
    filepath = param.filepath
    contents = param.contents
    max_chunk_size = param.maxChunkSize
    digest = param.digest

    # `index` is a shared counter, exactly like the closure variable in TS.
    # We read it and increment it in one synchronous step so concurrent
    # tasks assign non-overlapping indices in completion order.
    index_box: List[int] = [0]

    async def _resolve_chunk(
        chunk_without_id: ChunkWithoutID,
    ) -> Optional[Chunk]:

        if await count_tokens_async(chunk_without_id.content) > max_chunk_size:
            return None
        
        current_index = index_box[0]
        index_box[0] += 1

        return Chunk(
            content=chunk_without_id.content,
            start_line=chunk_without_id.start_line,
            end_line=chunk_without_id.end_line,
            signature=chunk_without_id.signature,
            other_metadata=chunk_without_id.other_metadata,
            digest=digest,
            filepath=filepath,
            index=current_index,
        )

    # Kick off all resolve tasks eagerly, exactly like `chunkPromises.push(...)`
    # in TS. Because we never await between creating the tasks here, every
    # chunk's token-count check runs concurrently.
    chunk_tasks: List[asyncio.Task] = []
    async for chunk_without_id in chunk_document_without_id(
        filepath, contents, max_chunk_size
    ):
        chunk_tasks.append(
            asyncio.create_task(_resolve_chunk(chunk_without_id))
        )

    # Iterate the tasks in creation order — matching `for await (const chunk
    # of chunkPromises)`. Each task already ran (or is running) concurrently,
    # so the `index` values they carry reflect completion order, while the
    # yield order reflects the original chunker's output order.
    for task in chunk_tasks:
        chunk = await task
        if chunk is None:
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
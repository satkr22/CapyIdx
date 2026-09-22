import asyncio
from typing import AsyncGenerator

from coreindexer.utils.count_tokens import count_tokens_async


async def basic_chunker(
    contents: str,
    max_chunk_size: int,
) -> AsyncGenerator[dict, None]:
    if len(contents.strip()) == 0:
        return

    chunk_content = ""
    chunk_tokens = 0
    start_line = 0
    curr_line = 0

    lines = contents.split("\n")

    async def _line_info(line: str) -> dict:
        return {
            "line": line,
            "token_count": await count_tokens_async(line),
        }

    line_tokens = await asyncio.gather(*(_line_info(l) for l in lines))

    for lt in line_tokens:
        if chunk_tokens + lt["token_count"] > max_chunk_size - 5:
            yield {
                "content": chunk_content,
                "startLine": start_line,
                "endLine": curr_line - 1,
            }
            chunk_content = ""
            chunk_tokens = 0
            start_line = curr_line

        if lt["token_count"] < max_chunk_size:
            chunk_content += f'{lt["line"]}\n'
            chunk_tokens += lt["token_count"] + 1

        curr_line += 1

    yield {
        "content": chunk_content,
        "startLine": start_line,
        "endLine": curr_line - 1,
    }
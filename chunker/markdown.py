"""
Python port of markdown.ts

Preserves the behaviour of the original TypeScript markdown chunker,
including the JS-template-literal stringification quirk where an
`undefined` header becomes the literal string "undefined".
"""

import re
from typing import Any, AsyncGenerator, Dict, List, Optional

from basic import basic_chunker
from utils.count_tokens import count_tokens


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _js_str(val: Any) -> str:
    """Mimic JS `${x}` stringification for undefined/null.

    In JS, `${undefined}` produces the literal string "undefined".
    The TS markdown chunker does `${section.header}` where section.header
    can be undefined, so we preserve that quirk exactly.
    """
    if val is None:
        return "undefined"
    return str(val)


# JS `\w` without the `u` flag is [A-Za-z0-9_].
# JS `\s` without the `u` flag is a Unicode whitespace class that Python's
# default `\s` almost matches (differences are exotic: \ufeff etc.).
_NON_WORD = re.compile(r"[^A-Za-z0-9_\-\s]")
_WHITESPACE_RUN = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Header / fragment cleaning
# ---------------------------------------------------------------------------

def clean_fragment(fragment: Optional[str]) -> Optional[str]:
    if not fragment:
        return None

    # Remove leading and trailing whitespaces
    fragment = fragment.strip()

    # If there's a ](, which would mean a link, remove everything after it
    paren_index = fragment.find("](")
    if paren_index != -1:
        fragment = fragment[:paren_index]

    # Remove all special characters except alphanumeric, hyphen, space, and underscore
    fragment = _NON_WORD.sub("", fragment).strip()

    # Convert to lowercase
    fragment = fragment.lower()

    # Replace spaces with hyphens
    fragment = _WHITESPACE_RUN.sub("-", fragment)

    return fragment


def clean_header(header: Optional[str]) -> Optional[str]:
    if not header:
        return None

    # Remove leading and trailing whitespaces
    header = header.strip()

    # If there's a (, remove everything after it
    paren_index = header.find("(")
    if paren_index != -1:
        header = header[:paren_index]

    # Remove all special characters except alphanumeric, hyphen, space, and underscore
    header = _NON_WORD.sub("", header).replace("¶", "").strip()

    return header


def find_header(lines: List[str]) -> Optional[str]:
    for line in lines:
        if line.startswith("#"):
            parts = line.split("# ")
            return parts[1] if len(parts) > 1 else None
    return None


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

async def markdown_chunker(
    content: str,
    max_chunk_size: int,
    h_level: int,
) -> AsyncGenerator[Dict[str, Any], None]:
    if count_tokens(content) <= max_chunk_size:
        header = find_header(content.split("\n"))
        yield {
            "content": content,
            "startLine": 0,
            "endLine": len(content.split("\n")),
            "otherMetadata": {
                "fragment": clean_fragment(header),
                "title": clean_header(header),
            },
        }
        return

    if h_level > 4:
        header = find_header(content.split("\n"))

        async for chunk in basic_chunker(content, max_chunk_size):
            yield {
                **chunk,
                "otherMetadata": {
                    "fragment": clean_fragment(header),
                    "title": clean_header(header),
                },
            }
        return

    h = "#" * (h_level + 1) + " "
    lines = content.split("\n")
    sections: List[Dict[str, Any]] = []

    current_section_start_line = 0
    current_section: List[str] = []

    for i, line in enumerate(lines):
        if line.startswith(h) or i == 0:
            if current_section:
                is_header = current_section[0].startswith(h)
                sections.append({
                    "header": current_section[0] if is_header else find_header(current_section),
                    "content": "\n".join(
                        current_section[1:] if is_header else current_section
                    ),
                    "startLine": current_section_start_line,
                    "endLine": current_section_start_line + len(current_section),
                })
            current_section = [line]
            current_section_start_line = i
        else:
            current_section.append(line)

    if current_section:
        is_header = current_section[0].startswith(h)
        sections.append({
            "header": current_section[0] if is_header else find_header(current_section),
            "content": "\n".join(
                current_section[1:] if is_header else current_section
            ),
            "startLine": current_section_start_line,
            "endLine": current_section_start_line + len(current_section),
        })

    for section in sections:
        section_header = section["header"]
        header_tokens = count_tokens(section_header) if section_header else 0

        async for chunk in markdown_chunker(
            section["content"],
            max_chunk_size - header_tokens,
            h_level + 1,
        ):
            chunk_meta = chunk.get("otherMetadata") or {}
            yield {
                "content": f'{_js_str(section_header)}\n{chunk["content"]}',
                "startLine": section["startLine"] + chunk["startLine"],
                "endLine": section["startLine"] + chunk["endLine"],
                "otherMetadata": {
                    "fragment": chunk_meta.get("fragment") or clean_fragment(section_header),
                    "title": chunk_meta.get("title") or clean_header(section_header),
                },
            }


# NOTE: Recursively chunks by header level (h1-h6).
# The final chunk will always include all parent headers.
# TODO: Merge together neighboring chunks if their sum doesn't exceed maxChunkSize
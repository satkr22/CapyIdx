from __future__ import annotations
from typing import Any

_LANG = {
    "py": "python", "pyi": "python",
    "ts": "typescript", "tsx": "tsx",
    "js": "javascript", "jsx": "jsx", "mjs": "javascript", "cjs": "javascript",
    "go": "go", "rs": "rust", "java": "java", "kt": "kotlin", "scala": "scala",
    "rb": "ruby", "php": "php", "cs": "csharp",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "h": "c", "c": "c",
    "swift": "swift", "sh": "bash", "bash": "bash", "zsh": "bash",
    "sql": "sql", "yaml": "yaml", "yml": "yaml", "json": "json", "toml": "toml",
    "md": "markdown", "html": "html", "css": "css",
}


def _lang(path: str) -> str:
    if "." not in path:
        return ""
    return _LANG.get(path.rsplit(".", 1)[-1].lower(), "")


def _is_error(item: Any) -> bool:
    return isinstance(item, dict) and "__xx_e_code" in item and "message" in item


def format_symbol(item: dict[str, Any]) -> str:
    """One symbol -> header line + fenced code block."""
    if _is_error(item):
        return f"[error {item['__xx_e_code']}] {item['message']}"

    path = item["path"]
    start = item["start_line"]
    end = item.get("returned_end_line", item["end_line"])
    name = item.get("symbol_name", "?")
    sid = item["symbol_id"]

    lines = [
        f"{path}:{start}-{end}",
        f"symbol: {name}",
        f"id: {sid}",
    ]
    if item.get("truncated"):
        total = item.get("total_lines", "?")
        lines.append(
            f"[truncated — showing lines {start}-{end} of {total}; "
            f"call get_symbol_range with next start_line/end_line to read the rest]"
        )
    header = "  ".join(lines[:2]) + "\n" + lines[2] + (
        "\n" + "\n".join(lines[3:]) if len(lines) > 3 else ""
    )

    body = f"```{_lang(path)}\n{item['code']}\n```"
    return f"{header}\n{body}"


def format_symbols(items: list[dict[str, Any]]) -> str:
    return "\n\n---\n\n".join(format_symbol(i) for i in items)


def format_lookup(result: dict[str, list[dict[str, object]]] | list[dict[str, object]]) -> str:
    """{query: [matches...]} -> compact list the LLM can pick IDs from."""
    if isinstance(result, list):  # single-item error case
        return "\n".join(
            format_symbol(i) if isinstance(i, dict) else str(i) for i in result
        )

    blocks: list[str] = []
    for query, _list in result.items():
        lines = [f"query: {query}"]
        for m in _list:
            if _is_error(m):
                lines.append(f"  [error {m['__xx_e_code']}] {m['message']}")
            else:
                name = m.get("symbol_name", "")
                tag = f"  ({name})" if name else ""
                lines.append(
                    f"  - {m['path']}:{m['start_line']}-{m['end_line']}"
                    f"  id={m['symbol_id']}{tag}"
                )
                signature = m.get("signature")
                if isinstance(signature, str):
                    sig = signature.strip().splitlines()
                    lines.append(
                        f"  signature={sig}"
                    )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(no matches)"
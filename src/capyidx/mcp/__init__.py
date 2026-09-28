"""MCP integration for CapyIdx.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from capyidx.mcp.runtime import Runtime, build_runtime
    from capyidx.mcp.tools import Tool, TOOLS, TOOLS_BY_NAME

__all__ = ["Runtime", "build_runtime", "TOOLS", "TOOLS_BY_NAME", "Tool"]


def __getattr__(name: str) -> Any:
    """Load public MCP objects only when they are explicitly requested."""
    if name in {"Runtime", "build_runtime"}:
        from capyidx.mcp.runtime import Runtime, build_runtime

        return {"Runtime": Runtime, "build_runtime": build_runtime}[name]
    if name in {"TOOLS", "TOOLS_BY_NAME", "Tool"}:
        from capyidx.mcp.tools import TOOLS, TOOLS_BY_NAME, Tool

        return {"TOOLS": TOOLS, "TOOLS_BY_NAME": TOOLS_BY_NAME, "Tool": Tool}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

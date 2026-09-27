"""CapyIdx MCP stdio server.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable

from mcp.server import Server, ServerRequestContext, stdio
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    Tool,
)

from capyidx.mcp.runtime import Runtime, build_runtime
from capyidx.mcp.tools import TOOLS, TOOLS_BY_NAME


_SOURCE_ROOT = str(Path(__file__).resolve().parents[2])
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

REPO_ENV_VARS = ("CAPYIDX_REPO", "CAPYIDX_ROOT")
SERVER_NAME = "capyidx"
SERVER_VERSION = "0.1.3"


def _repository_path() -> str:
    for name in REPO_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return os.getcwd()

async def _execute_tool(
    runtime: Runtime,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }

    try:
        return await tool.execute(runtime, arguments)
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"Internal error: {exc}"}],
            "isError": True,
        }


def _initial_index_tasks() -> list[asyncio.Task[Any]]:
    """Find the startup tasks runtime.py cannot retain itself."""
    tasks: list[asyncio.Task[Any]] = []
    for task in asyncio.all_tasks():
        if task.done():
            continue
        try:
            qualname = task.get_coro().__qualname__ # type: ignore
        except AttributeError:
            continue
        if qualname.endswith("build_runtime.<locals>.initial_index"):
            tasks.append(task)
    return tasks


async def _shutdown_runtime(
    runtime: Runtime,
    startup_tasks: list[asyncio.Task[Any]],
) -> None:
    """Cancel initial indexing before runtime.shutdown closes SQLite."""
    for task in startup_tasks:
        if not task.done():
            task.cancel()
    if startup_tasks:
        await asyncio.gather(*startup_tasks, return_exceptions=True)
    await runtime.shutdown()


class _NotificationState:
    def __init__(self) -> None:
        self.initialized = False
        self.session: Any | None = None
        self.send: Callable[[dict[str, Any]], Awaitable[None]] | None = None


async def _send_sdk_notification(session: Any, data: Any) -> None:
    """Send progress through the SDK's typed logging notification API."""
    if session is None:
        return

    send_log_message = getattr(session, "send_log_message", None)
    if send_log_message is not None:
        await send_log_message(level="info", data=data)
        return

    from mcp.types import (  # type: ignore[import-not-found]
        LoggingMessageNotification,
        LoggingMessageNotificationParams,
    )

    await session.send_notification(
        LoggingMessageNotification(
            params=LoggingMessageNotificationParams(level="info", data=data)
        )
    )


def _make_runtime_callback(
    state: _NotificationState,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def emit(notification: dict[str, Any]) -> None:
        if not state.initialized:
            return

        data = notification.get("params", {}).get("data", notification)
        if state.send is not None:
            await state.send(notification)
        elif state.session is not None:
            try:
                await _send_sdk_notification(state.session, data)
            except Exception:
                return

    return emit


async def _run_sdk() -> None:
    
    state = _NotificationState()

    @asynccontextmanager
    async def lifespan(server: Server) -> AsyncGenerator[dict[str, Any], None]:
        del server
        runtime = await build_runtime(
            _repository_path(), emit_message=_make_runtime_callback(state)
        )
        startup_tasks = _initial_index_tasks()
        try:
            yield {"runtime": runtime}
        finally:
            await _shutdown_runtime(runtime, startup_tasks)

    async def handle_list_tools(
        ctx: ServerRequestContext,
        params: Any,
    ) -> ListToolsResult:
        del params
        state.session = ctx.session
        state.initialized = True
        return ListToolsResult(
            tools=[
                Tool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
                for tool in TOOLS
            ]
        )

    async def handle_call_tool(
        ctx: ServerRequestContext,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        state.session = ctx.session
        state.initialized = True
        result = await _execute_tool(
            ctx.lifespan_context["runtime"],
            params.name,
            params.arguments or {},
        )
        return CallToolResult(**result)

    server = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        lifespan=lifespan,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )

    async with stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

async def run_stdio() -> None:
    await _run_sdk()


if __name__ == "__main__":
    asyncio.run(run_stdio())
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from capyidx.mcp import runtime as runtime_module
from capyidx.mcp import server


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_repository_path_prefers_supported_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.os, "environ", {"CAPYIDX_ROOT": "/root", "CAPYIDX_REPO": "/repo"})
    assert server._repository_path() == "/repo"
    monkeypatch.setattr(server.os, "environ", {"CAPYIDX_ROOT": "/root"})
    assert server._repository_path() == "/root"
    monkeypatch.setattr(server.os, "environ", {})
    monkeypatch.setattr(server.os, "getcwd", lambda: "/working")
    assert server._repository_path() == "/working"


def test_execute_tool_handles_unknown_success_and_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(_runtime: Any, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    tool = SimpleNamespace(execute=execute)
    monkeypatch.setattr(server, "TOOLS_BY_NAME", {"known": tool})

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        success = await server._execute_tool(object(), "known", {})
        unknown = await server._execute_tool(object(), "missing", {})

        async def exploding(_runtime: Any, _arguments: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("broken")

        tool.execute = exploding
        failure = await server._execute_tool(object(), "known", {})
        return success, unknown, failure

    success, unknown, failure = run(scenario())
    assert success["isError"] is False
    assert unknown == {
        "content": [{"type": "text", "text": "Unknown tool: missing"}],
        "isError": True,
    }
    assert failure == {
        "content": [{"type": "text", "text": "Internal error: broken"}],
        "isError": True,
    }


def test_send_sdk_notification_uses_typed_logging_when_available() -> None:
    calls: list[dict[str, Any]] = []

    class Session:
        async def send_log_message(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    run(server._send_sdk_notification(Session(), {"progress": 1}))
    assert calls == [{"level": "info", "data": {"progress": 1}}]


def test_send_sdk_notification_falls_back_to_typed_notification() -> None:
    calls: list[Any] = []

    class Session:
        async def send_notification(self, notification: Any) -> None:
            calls.append(notification)

    run(server._send_sdk_notification(Session(), {"message": "hello"}))
    assert len(calls) == 1
    assert calls[0].params.level == "info"
    assert calls[0].params.data == {"message": "hello"}


def test_send_sdk_notification_without_session_is_a_noop() -> None:
    run(server._send_sdk_notification(None, "ignored"))


def test_runtime_callback_obeys_initialization_and_prefers_custom_sender() -> None:
    state = server._NotificationState()
    sent: list[dict[str, Any]] = []
    state.send = lambda value: _record(sent, value)
    callback = server._make_runtime_callback(state)

    async def scenario() -> None:
        await callback({"method": "x", "params": {"data": "before"}})
        state.initialized = True
        await callback({"method": "x", "params": {"data": "after"}})

    run(scenario())
    assert sent == [{"method": "x", "params": {"data": "after"}}]


async def _record(target: list[dict[str, Any]], value: dict[str, Any]) -> None:
    target.append(value)


def test_runtime_callback_uses_session_and_swallows_notification_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def send(session: Any, data: Any) -> None:
        calls.append((session, data))

    monkeypatch.setattr(server, "_send_sdk_notification", send)
    state = server._NotificationState()
    state.initialized = True
    state.session = object()
    callback = server._make_runtime_callback(state)
    run(callback({"params": {"data": "payload"}}))
    assert calls == [(state.session, "payload")]

    async def fail(_session: Any, _data: Any) -> None:
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr(server, "_send_sdk_notification", fail)
    run(callback({"params": {"data": "ignored"}}))


def test_runtime_callback_uses_whole_notification_when_data_is_absent() -> None:
    calls: list[Any] = []

    async def send(_session: Any, data: Any) -> None:
        calls.append(data)

    state = server._NotificationState()
    state.initialized = True
    state.session = object()
    callback = server._make_runtime_callback(state)
    # No custom sender: the callback's session path receives the fallback data.
    original = server._send_sdk_notification
    try:
        server._send_sdk_notification = send  # type: ignore[assignment]
        run(callback({"method": "notification", "params": {}}))
    finally:
        server._send_sdk_notification = original
    assert calls == [{"method": "notification", "params": {}}]


def test_runtime_shutdown_cancels_tasks_drains_cache_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def wait_forever() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            order.append("cancelled")

    class Connection:
        def close(self) -> None:
            order.append("closed")

    async def drain(timeout: float) -> None:
        order.append(f"drain:{timeout}")

    monkeypatch.setattr(runtime_module, "drain_warm_tasks", drain)

    async def scenario() -> None:
        tasks = [asyncio.create_task(wait_forever()) for _ in range(3)]
        runtime = runtime_module.Runtime(
            indexer=object(),
            lookup=object(),
            conn=Connection(),
            roots=[],
            watch_task=tasks[0],
            branch_tasks=tasks[1:],
            initial_index_task=tasks[2],
        )
        await runtime.shutdown()
        assert tasks[0].cancelled() or tasks[0].done()
        assert all(task.done() for task in tasks)

    run(scenario())
    assert "drain:2.0" in order
    assert order[-1] == "closed"


@dataclass
class Update:
    status: str
    progress: float = 1.0
    desc: str = "done"


class BuildIndexer:
    def __init__(self, **_kwargs: Any) -> None:
        self._watch_started = asyncio.Event()
        self.current_indexing_state = Update("done")
        self.system_ready = False
        self.ready_values: list[bool] = []

    def set_system_ready(self, value: bool) -> None:
        self.system_ready = value
        self.ready_values.append(value)

    async def start_watch(self, **_kwargs: Any):
        self._watch_started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def wait_for_watch_restart(self) -> bool:
        return False

    async def watch_git_head(self, *_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    async def refresh_codebase_index(self, *_args: Any):
        yield Update("done")


class ConnectionForBuild:
    def close(self) -> None:
        pass


def test_build_runtime_starts_watchers_indexing_and_progress_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    async def setup(_repo: Any) -> tuple[object, list[str], list[str]]:
        return object(), ["root-a", "root-b"], ["tag"]

    async def emit(value: dict[str, Any]) -> None:
        captured.append(value)

    monkeypatch.setattr(runtime_module, "_setup", setup)
    monkeypatch.setattr(runtime_module, "open_index", lambda _tags: ConnectionForBuild())
    monkeypatch.setattr(runtime_module, "ChunkCodebaseIndex", lambda **_kwargs: object())
    monkeypatch.setattr(runtime_module, "CodeIndexer", BuildIndexer)
    monkeypatch.setattr(runtime_module, "SymbolLookup", lambda _conn, _roots: object())

    async def scenario() -> tuple[runtime_module.Runtime, BuildIndexer]:
        value = await runtime_module.build_runtime("/repo", emit_message=emit, flush_interval=0.25)
        await asyncio.sleep(0)
        indexer = value.indexer
        await value.shutdown()
        return value, indexer

    runtime, indexer = run(scenario())
    assert runtime.roots == ["root-a", "root-b"]
    assert len(runtime.branch_tasks) == 2
    assert indexer.ready_values == [False, True]
    assert captured[0]["method"] == "notifications/message"
    assert captured[0]["params"]["data"] == {
        "status": "done",
        "progress": 1.0,
        "desc": "done",
    }


def test_main_and_run_stdio_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run_stdio() -> None:
        calls.append("stdio")

    monkeypatch.setattr(server, "run_stdio", fake_run_stdio)
    server.main()
    assert calls == ["stdio"]
    monkeypatch.undo()

    async def fake_sdk() -> None:
        calls.append("sdk")

    monkeypatch.setattr(server, "_run_sdk", fake_sdk)
    run(server.run_stdio())
    assert calls == ["stdio", "sdk"]


from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, List, Protocol, Set
from urllib.parse import quote

from capyidx.utils.ignore import Ignore, default_ignore_file_and_dir, git_ig_array_from_file_path
from capyidx.utils.uri import get_uri_to_path
from capyidx.utils.uri2 import join_paths_to_uri

#  native backend
try:
    from watchdog.events import FileSystemEventHandler as _WatchdogEventHandlerImpl
    from watchdog.observers import Observer

    _WatchdogEventHandler: Any = _WatchdogEventHandlerImpl
    _WATCHDOG_AVAILABLE = True
except Exception:
    class _FallbackWatchdogEventHandler:
        """Fallback base when the optional watchdog dependency is absent."""
        pass

    _WatchdogEventHandler: Any = _FallbackWatchdogEventHandler
    Observer = None
    _WATCHDOG_AVAILABLE = False


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def path_to_uri(path: str) -> str:
    """Convert local path to file:// URI."""
    return Path(path).resolve().as_uri()


def is_hidden(path: str) -> bool:
    return any(part.startswith(".") for part in Path(path).parts)


# ---------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------

class FileWatcher(Protocol):
    def watch(self, roots: List[str]) -> AsyncIterator[List[str]]:
        """
        Yield batches of changed file URIs.
        """
        ...

# ---------------------------------------------------------------------
# Native Watchdog implementation
# ---------------------------------------------------------------------

class _WatchdogHandler(_WatchdogEventHandler):
    def __init__(self, queue: asyncio.Queue[str], loop, roots: List[str]):
        self.queue = queue
        self.loop = loop
        self.ignore = Ignore()
        self.ignore.add(default_ignore_file_and_dir)
        for root in roots:
            p = join_paths_to_uri(root, ".gitignore")
            if Path(p).exists:
                self.ignore.add(git_ig_array_from_file_path(p))

    def _push(self, path: str):
        if os.path.isdir(path):
            return

        if self.ignore.ignores(path):
            return
        
        self.loop.call_soon_threadsafe(
            self.queue.put_nowait,
            path_to_uri(path),
        )

    def on_created(self, event):
        self._push(event.src_path)

    def on_modified(self, event):
        self._push(event.src_path)

    def on_deleted(self, event):
        self._push(event.src_path)

    def on_moved(self, event):
        self._push(event.dest_path)


class WatchdogFileWatcher:
    """
    Native filesystem watcher using watchdog.
    Automatically uses inotify/FSEvents/Windows APIs.
    """

    def __init__(
        self,
        debounce_ms: int = 250,
        ignore_hidden: bool = True,
    ):
        self.debounce_ms = debounce_ms
        self.ignore_hidden = ignore_hidden

    async def watch(self, roots: List[str]) -> AsyncIterator[List[str]]:
        # print("autowatcher in")
        if not _WATCHDOG_AVAILABLE:
            raise RuntimeError(
                "watchdog not installed. Use PollingFileWatcher."
            )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()

        # handler = _WatchdogHandler(queue, loop, roots)
        handler = _WatchdogHandler(queue, loop, roots)
        assert Observer is not None
        observer = Observer()

        for root in roots:
            path = Path(root.replace("file://", ""))
            observer.schedule(handler, str(path), recursive=True)

        observer.start()

        try:
            while True:
                first = await queue.get()

                batch: Set[str] = {first}

                deadline = (
                    time.monotonic()
                    + self.debounce_ms / 1000
                )

                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break

                    try:
                        nxt = await asyncio.wait_for(
                            queue.get(),
                            timeout=remaining,
                        )
                        batch.add(nxt)
                    except asyncio.TimeoutError:
                        break

                files = sorted(batch)

                if self.ignore_hidden:
                    files = [
                        f
                        for f in files
                        if not is_hidden(
                            Path(f.replace("file://", "")).as_posix()
                        )
                    ]

                if files:
                    yield files

        finally:
            observer.stop()
            observer.join()


# ---------------------------------------------------------------------
# polling fallback
# ---------------------------------------------------------------------

class PollingFileWatcher:
    """
    Scans modification times every poll interval.
    """

    def __init__(
        self,
        poll_interval: float = 1.0,
        ignore_hidden: bool = True,
    ):
        self.poll_interval = poll_interval
        self.ignore_hidden = ignore_hidden
        self._state: dict[str, float] = {}

    def _scan(self, roots: List[str], report_new: bool = True) -> List[str]:
        changed = []
        current: dict[str, float] = {}

        for root in roots:
            root_path = Path(root.replace("file://", ""))
            if not root_path.exists():
                continue

            for path in root_path.rglob("*"):
                if not path.is_file():
                    continue
                if self.ignore_hidden and is_hidden(str(path)):
                    continue
                try:
                    mtime = path.stat().st_mtime_ns
                except OSError:
                    continue

                uri = path.as_uri()
                current[uri] = mtime

                previous = self._state.get(uri)

                if previous is None:
                    if report_new:
                        changed.append(uri)
                elif previous != mtime:
                    changed.append(uri)

        # Detect deleted files
        deleted = set(self._state) - set(current)
        changed.extend(sorted(deleted))

        self._state = current

        return sorted(set(changed))

    async def watch(
        self, roots: List[str]
    ) -> AsyncIterator[List[str]]:
        # Initial snapshot: populate state without reporting anything
        self._scan(roots, report_new=False)

        while True:
            await asyncio.sleep(self.poll_interval)

            changed = self._scan(roots, report_new=True)

            if changed:
                yield changed

# ---------------------------------------------------------------------
# Auto chooser
# ---------------------------------------------------------------------

class AutoFileWatcher:
    """
    Uses watchdog if installed, otherwise polling.
    """

    def __init__(
        self,
        debounce_ms: int = 250,
        poll_interval: float = 1.0,
    ):
        
        if _WATCHDOG_AVAILABLE:
            
            self._impl: FileWatcher = WatchdogFileWatcher(
                debounce_ms=debounce_ms
            )
        else:
            self._impl = PollingFileWatcher(
                poll_interval=poll_interval,
            )

    async def watch(self, roots: List[str]) -> AsyncIterator[List[str]]:
        async for files in self._impl.watch(roots):
            yield files

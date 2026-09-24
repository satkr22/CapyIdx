from __future__ import annotations

from collections import OrderedDict, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

from capyidx.retrieval.models import SymbolCode


def canonical_path(path: str) -> str:
    """Return one comparable key for local paths and file:// URIs."""
    value = str(path)
    if value.startswith("file://"):
        value = unquote(urlparse(value).path)
    return str(Path(value).expanduser().resolve(strict=False))


class SymbolCache:
    """Byte-bounded LRU cache for reconstructed symbols.

    The path index is maintained together with the LRU map so invalidation
    remains bounded and removes every symbol belonging to a changed file.
    """

    def __init__(self, max_bytes: int):
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, tuple[str, SymbolCode]] = OrderedDict()
        self._by_path: dict[str, set[str]] = defaultdict(set)
        self._current_bytes = 0

    @property
    def current_bytes(self) -> int:
        return self._current_bytes

    def get(self, symbol_id: str) -> SymbolCode | None:
        entry = self._entries.get(symbol_id)
        if entry is None:
            return None
        self._entries.move_to_end(symbol_id)
        return entry[1]

    def put(self, symbol_id: str, path: str, code: SymbolCode) -> None:
        size = len(code.code.encode("utf-8"))
        previous = self._entries.pop(symbol_id, None)
        if previous is not None:
            previous_path, previous_code = previous
            self._remove_from_path(previous_path, symbol_id)
            self._current_bytes -= len(previous_code.code.encode("utf-8"))

        key = canonical_path(path)
        self._entries[symbol_id] = (key, code)
        self._by_path[key].add(symbol_id)
        self._current_bytes += size

        while self._current_bytes > self.max_bytes and self._entries:
            oldest_id, (oldest_path, oldest_code) = self._entries.popitem(last=False)
            self._current_bytes -= len(oldest_code.code.encode("utf-8"))
            self._remove_from_path(oldest_path, oldest_id)

    def clear(self) -> None:
        """Drop all cached symbols and path-index entries."""
        self._entries.clear()
        self._by_path.clear()
        self._current_bytes = 0

    def invalidate_path(self, path: str) -> None:
        key = canonical_path(path)
        for symbol_id in tuple(self._by_path.get(key, ())):
            entry = self._entries.pop(symbol_id, None)
            if entry is not None:
                self._current_bytes -= len(entry[1].code.encode("utf-8"))
        self._by_path.pop(key, None)

    def _remove_from_path(self, path: str, symbol_id: str) -> None:
        ids = self._by_path.get(path)
        if ids is None:
            return
        ids.discard(symbol_id)
        if not ids:
            del self._by_path[path]

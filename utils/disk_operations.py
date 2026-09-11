# disk_operations.py
import asyncio
import os
from typing import List, Tuple
from urllib.parse import quote, unquote, urlparse


FILE = 1
DIRECTORY = 2
SYMBOLIC_LINK = 64

Entry = Tuple[str, int]

_WINDOWS = os.name == "nt"


def _path_to_uri(path: str) -> str:
    p = os.path.abspath(path).replace(os.sep, "/")
    if _WINDOWS and not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p, safe="/:")


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"not a file:// uri: {uri!r}")
    p = unquote(parsed.path)
    if _WINDOWS and len(p) >= 3 and p[0] == "/" and p[2] == ":":
        p = p[1:]
    return p


def _classify(entry: os.DirEntry) -> int:
    # is_symlink() must come first: is_dir()/is_file() follow the link,
    # and a misreported symlink makes the walker recurse forever.
    if entry.is_symlink():
        return SYMBOLIC_LINK
    if entry.is_dir(follow_symlinks=False):
        return DIRECTORY
    if entry.is_file(follow_symlinks=False):
        return FILE
    return -1


class DiskOperations:
    """Filesystem backend for the walker. Three async methods, no more."""

    def __init__(self, roots: List[str], encoding: str = "utf-8") -> None:
        self._roots: List[str] = [_path_to_uri(r) for r in roots]
        self._encoding = encoding

    async def get_workspace_dirs(self) -> List[str]:
        return list(self._roots)

    async def list_dir(self, uri: str) -> List[Entry]:
        return await asyncio.to_thread(self._list_dir_sync, _uri_to_path(uri))

    async def read_file(self, uri: str) -> str:
        return await asyncio.to_thread(self._read_file_sync, _uri_to_path(uri))

    # --- sync workers (off the event loop) --------------------------------

    @staticmethod
    def _list_dir_sync(path: str) -> List[Entry]:
        out: List[Entry] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    t = _classify(entry)
                    if t != -1:
                        out.append((entry.name, t))
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            return []
        return out

    def _read_file_sync(self, path: str) -> str:
        try:
            with open(path, "r", encoding=self._encoding, errors="replace") as f:
                return f.read()
        except (OSError, IsADirectoryError):
            return ""
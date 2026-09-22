# disk_operations.py
import os
import asyncio
import subprocess
from pathlib import Path
from enum import IntEnum
from typing import List, Tuple, Optional

from coreindexer.base.index_d import FileSystem, FileType
from urllib.parse import quote, unquote, urlparse

from coreindexer.utils.uri import get_uri_to_path, get_path_to_uri

Entry = Tuple[str, FileType]

_WINDOWS = os.name == "nt"


def _classify(entry: os.DirEntry) -> int:
    # is_symlink() must come first: is_dir()/is_file() follow the link,
    # and a misreported symlink makes the walker recurse forever.
    if entry.is_symlink():
        return FileType.SYMBOLIC_LINK
    if entry.is_dir(follow_symlinks=False):
        return FileType.DIRECTORY
    if entry.is_file(follow_symlinks=False):
        return FileType.FILE
    return -1


class DiskOperations(FileSystem):
    """Filesystem backend for the walker. async methods."""

    def __init__(self, roots: List[str], encoding: str = "utf-8") -> None:
        self._roots: List[str] = [get_path_to_uri(r) if not r.startswith("file://") else r for r in roots]
        self._encoding = encoding

    async def get_workspace_dirs(self) -> List[str]:
        return list(self._roots)

    async def list_dir(self, uri: str) -> List[Entry]:
        return await asyncio.to_thread(self._list_dir_sync, get_uri_to_path(uri))

    async def read_file(self, uri: str) -> str:
        return await asyncio.to_thread(self._read_file_sync, get_uri_to_path(uri) if uri.startswith("file://") else uri)
    
    async def file_exists(self, uri: str) -> bool:
        return await asyncio.to_thread(os.path.exists, get_uri_to_path(uri))
    
    async def get_file_stats(self, uris: List[str]) -> dict:
        """Return {path: FileStats-like dict with size + last_modified ms}.

        Keys match the input paths (after normalization onlyfor lookup).
        """
        return await asyncio.to_thread(self._get_file_stats_sync, [get_uri_to_path(uri) for uri in uris])
    
    async def get_branch(self, directory_uri: str) -> str:
        return await asyncio.to_thread(self._get_branch_sync, get_uri_to_path(directory_uri))

    async def get_repo_name(self, directory_uri: str) -> Optional[str]:
        return await asyncio.to_thread(
            self._get_repo_name_sync, get_uri_to_path(directory_uri)
        )
    

    # -----------------------------------
    @staticmethod
    def _require_file_uri(uri: str) -> str:
        if not uri.startswith("file://"):
            raise ValueError(f"expected file:// URI, got {uri!r}")
        return uri

    @staticmethod
    def _list_dir_sync(path: str) -> List[Entry]:
        out: List[Entry] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    t = _classify(entry)
                    if t != -1:
                        out.append((entry.name, FileType(t)))
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            return []
        return out

    def _read_file_sync(self, path: str) -> str:
        try:
            with open(path, "r", encoding=self._encoding, errors="replace") as f:
                return f.read()
        except (OSError, IsADirectoryError):
            return ""
        
        
    def _get_file_stats_sync(self, paths: List[str]) -> dict:
        try:
            from coreindexer.base.index_d import FileStats
        except ImportError:
            FileStats = None  # type: ignore

        result = {}
        for raw in paths:
            p = raw
            try:
                st = os.stat(p, follow_symlinks=False)
                size = st.st_size
                # epoch milliseconds (matches Continue FileStats.lastModified)
                last_modified = int(st.st_mtime * 1000)
                if FileStats is not None:
                    result[raw] = FileStats(size=size, last_modified=last_modified)
                else:
                    result[raw] = {"size": size, "last_modified": last_modified}
            except OSError:
                continue
        return result

    @staticmethod
    def _get_branch_sync(directory: str) -> str:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=directory,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return out.strip() or "HEAD"
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return "NONE"

    @staticmethod
    def _get_repo_name_sync(directory: str) -> Optional[str]:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=directory,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            top = out.strip()
            return Path(top).name if top else None
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return None     
        
        
        

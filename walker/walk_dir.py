# walk_dir.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import (
    AsyncGenerator,
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
)

import pathspec  # pip install pathspec

# Sibling module imports (mirroring the TS file's imports)
from utils.uri import join_paths_to_uri
from utils.ignore import Ignore, default_ignore_file_and_dir, git_ig_array_from_file
from utils.disk_operations import DiskOperations

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class FileType(IntEnum):
    # Matches VSCode's FileType enum values used by the TS code
    FILE = 1
    DIRECTORY = 2
    SYMBOLIC_LINK = 64


Entry = Tuple[str, FileType]


# class IDE(Protocol):
#     async def list_dir(self, uri: str) -> List[Entry]: ...
#     async def read_file(self, uri: str) -> str: ...
#     async def get_workspace_dirs(self) -> List[str]: ...


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass
class WalkerOptions:
    include: Optional[str] = None                 # "dirs" | "files" | "both"
    return_relative_uris_paths: Optional[bool] = None
    source: Optional[str] = None
    override_default_ignores: Optional[Ignore] = None
    recursive: Optional[bool] = None


_DEFAULT_OPTIONS: dict = {
    "include": "files",
    "return_relative_uris_paths": False,
    "source": None,
    "override_default_ignores": None,
    "recursive": True,
}


def _resolve_options(overrides: Optional[WalkerOptions]) -> dict:
    """Equivalent to `{ ...defaultOptions, ...overrides }` in TS."""
    opts = dict(_DEFAULT_OPTIONS)
    if overrides is not None:
        for key in _DEFAULT_OPTIONS:
            value = getattr(overrides, key)
            if value is not None:
                opts[key] = value
    return opts


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

LIST_DIR_CACHE_TIME = 30_000      # ms
IGNORE_FILE_CACHE_TIME = 30_000   # ms


def _now_ms() -> int:
    return int(time.time() * 1000)


class WalkDirCache:
    def __init__(self) -> None:
        # uri -> {"time": int, "entries": asyncio.Task}
        self.dir_list_cache: dict = {}
        # uri -> {"time": int, "ignore": asyncio.Task}
        self.dir_ignore_cache: dict = {}

    # The super safe approach for now
    def invalidate(self) -> None:
        self.dir_list_cache.clear()
        self.dir_ignore_cache.clear()


# TODO - singleton approach better?
walk_dir_cache = WalkDirCache()


# ---------------------------------------------------------------------------
# DFS walker
# ---------------------------------------------------------------------------

class DFSWalker:
    def __init__(self, uri: str, diskop: DiskOperations, options: dict) -> None:
        self.uri = uri
        self.diskop = diskop
        self.options = options

    # walk is a depth-first search implementation
    async def walk(self) -> AsyncGenerator[str, None]:
        start = _now_ms()
        ignore_file_time = 0
        ignore_time = 0
        list_dir_time = 0
        dirs = 0
        list_dir_cache_hits = 0
        ignore_cache_hits = 0

        section = _now_ms()
        default_and_global_ignores = Ignore()
        default_and_global_ignores.add(
            self.options["override_default_ignores"]
            if self.options["override_default_ignores"] is not None
            else default_ignore_file_and_dir
        )
        
        ignore_file_time += _now_ms() - section

        root_context = {
            "walkable_entry": {
                "name": "",
                "relative_uri_path": "",
                "uri": self.uri,
                "type": FileType.DIRECTORY,
                "entry": ("", FileType.DIRECTORY),
            },
            "ignore_contexts": [],
        }
        stack = [root_context]

        while stack:
            cur = stack.pop()

            # Only directories will be added to the stack
            dirs += 1

            section = _now_ms()
            entries: List[Entry] = []
            cached_listdir = walk_dir_cache.dir_list_cache.get(
                cur["walkable_entry"]["uri"]
            )

            if (
                cached_listdir
                and cached_listdir["time"] > _now_ms() - LIST_DIR_CACHE_TIME
            ):
                print("CACHE HIT:", cur["walkable_entry"]["uri"])
                entries = await cached_listdir["entries"]
                list_dir_cache_hits += 1
            else:
                task = asyncio.create_task(
                    self.diskop.list_dir(cur["walkable_entry"]["uri"])
                )
                walk_dir_cache.dir_list_cache[cur["walkable_entry"]["uri"]] = {
                    "time": _now_ms(),
                    "entries": task,
                }
                entries = await task # type: ignore
            list_dir_time += _now_ms() - section

            section = _now_ms()
            cached_ignore = walk_dir_cache.dir_ignore_cache.get(
                cur["walkable_entry"]["uri"]
            )
            if (
                cached_ignore
                and cached_ignore["time"] > _now_ms() - IGNORE_FILE_CACHE_TIME
            ):
                new_ignore = await cached_ignore["ignore"]
                ignore_cache_hits += 1
            else:
                ignore_task = asyncio.create_task(
                    get_ignore_context(
                        cur["walkable_entry"]["uri"],
                        entries,
                        self.diskop,
                        default_and_global_ignores,
                    )
                )
                walk_dir_cache.dir_ignore_cache[cur["walkable_entry"]["uri"]] = {
                    "time": _now_ms(),
                    "ignore": ignore_task,
                }
                new_ignore = await ignore_task

            ignore_contexts = cur["ignore_contexts"] + [
                {
                    "ignore": new_ignore,
                    "dirname": cur["walkable_entry"]["relative_uri_path"],
                }
            ]
            ignore_file_time += _now_ms() - section

            for entry in entries:
                if self._entry_is_symlink(entry):
                    # If called from the root, a symlink either links to a real
                    # file in this repository, and therefore will be walked OR
                    # it links to something outside of the repository and we do
                    # not want to index it
                    continue

                relative = cur["walkable_entry"]["relative_uri_path"]
                walkable_entry = {
                    "name": entry[0],
                    "relative_uri_path": (
                        f"{relative}{'/' if relative else ''}{entry[0]}"
                    ),
                    "uri": join_paths_to_uri(
                        cur["walkable_entry"]["uri"], entry[0]
                    ),
                    "type": entry[1],
                    "entry": entry,
                }

                rel_path = walkable_entry["relative_uri_path"]
                if self._entry_is_directory(entry):
                    rel_path = f"{rel_path}/"
                else:
                    if self.options["include"] == "dirs":
                        continue

                should_ignore = False
                for ig in ignore_contexts:
                    if should_ignore:
                        continue
                    # remove the directory name and path separator from the
                    # match path, unless this is an ignore file in the root
                    # directory
                    prefix_length = (
                        0 if len(ig["dirname"]) == 0 else len(ig["dirname"]) + 1
                    )
                    # The ignore library expects a path relative to the ignore
                    # file location
                    match_path = rel_path[prefix_length:]
                    section = _now_ms()
                    if ig["ignore"].ignores(match_path):
                        should_ignore = True
                    ignore_time += _now_ms() - section

                if should_ignore:
                    continue

                if self._entry_is_directory(entry):
                    if self.options["recursive"]:
                        stack.append(
                            {
                                "walkable_entry": walkable_entry,
                                "ignore_contexts": ignore_contexts,
                            }
                        )
                    if self.options["include"] != "files":
                        # if yielding dirs or both, walker includes relative paths
                        trailing_slash = (
                            "" if self.options["include"] == "dirs" else "/"
                        )
                        if self.options["return_relative_uris_paths"]:
                            yield walkable_entry["relative_uri_path"] + trailing_slash
                        else:
                            yield walkable_entry["uri"] + trailing_slash
                elif self.options["include"] != "dirs":
                    if self.options["return_relative_uris_paths"]:
                        yield walkable_entry["relative_uri_path"]
                    else:
                        yield walkable_entry["uri"]

        # print(
        #     f"Walk Dir Result:\n"
        #     f"Source: {self.options['source'] or 'unknown'}\n"
        #     f"Dir: {self.uri}\n"
        #     f"Duration: {_now_ms() - start}ms:\n"
        #     f"\tList dir: {list_dir_time}ms "
        #     f"({list_dir_cache_hits}/{dirs} cache hits)\n"
        #     f"\tIgnore files: {ignore_file_time}ms "
        #     f"({ignore_cache_hits}/{dirs} cache hits)\n"
        #     f"\tIgnoring: {ignore_time}ms"
        # )

    def _entry_is_directory(self, entry: Entry) -> bool:
        return entry[1] == FileType.DIRECTORY

    def _entry_is_symlink(self, entry: Entry) -> bool:
        return entry[1] == FileType.SYMBOLIC_LINK


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def walk_dir_async(
    path: str,
    diskop: DiskOperations,
    option_overrides: Optional[WalkerOptions] = None,
) -> AsyncGenerator[str, None]:
    options = _resolve_options(option_overrides)
    async for p in DFSWalker(path, diskop, options).walk():
        yield p


async def walk_dir(
    uri: str,
    diskop: DiskOperations,
    option_overrides: Optional[WalkerOptions] = None,
) -> List[str]:
    uris_or_relative_paths: List[str] = []
    async for p in walk_dir_async(uri, diskop, option_overrides):
        uris_or_relative_paths.append(p)
    return uris_or_relative_paths


async def walk_dirs(
    diskop: DiskOperations,
    option_overrides: Optional[WalkerOptions] = None,
    dirs: Optional[List[str]] = None,  # Can pass dirs to prevent duplicate calls
) -> List[str]:
    workspace_dirs = dirs if dirs is not None else await diskop.get_workspace_dirs()
    results = await asyncio.gather(
        *[walk_dir(d, diskop, option_overrides) for d in workspace_dirs]
    )
    return [item for sublist in results for item in sublist]


# ---------------------------------------------------------------------------
# Ignore context
# ---------------------------------------------------------------------------

async def get_ignore_context(
    current_dir: str,
    current_dir_entries: List[Entry],
    diskop: DiskOperations,
    default_and_global_ignores: Ignore,
) -> Ignore:
    dir_files = [
        name
        for name, entry_type in current_dir_entries
        if entry_type == FileType.FILE
    ]

    # Find ignore files and get ignore arrays from their contexts.
    # These are done separately so that .continueignore can override .gitignore
    git_ignore_file = ".gitignore" if ".gitignore" in dir_files else None
    continue_ignore_file = (
        ".continueignore" if ".continueignore" in dir_files else None
    )

    async def get_git_ignore_patterns():
        if git_ignore_file:
            contents = await diskop.read_file(f"{current_dir}/.gitignore")
            return git_ig_array_from_file(contents)
        return []

    async def get_continue_ignore_patterns():
        if continue_ignore_file:
            contents = await diskop.read_file(f"{current_dir}/.continueignore")
            return git_ig_array_from_file(contents)
        return []

    ignore_arrays = await asyncio.gather(
        get_git_ignore_patterns(),
        get_continue_ignore_patterns(),
    )

    if len(ignore_arrays[0]) == 0 and len(ignore_arrays[1]) == 0:
        return default_and_global_ignores

    # Note precedence here!
    ignore_context = Ignore()
    ignore_context.add(ignore_arrays[0])          # gitignore
    ignore_context.add(default_and_global_ignores)  # default file/folder ignores
                                                    # followed by global
                                                    # .continueignore - combined
                                                    # for speed
    ignore_context.add(ignore_arrays[1])          # local .continueignore
    return ignore_context
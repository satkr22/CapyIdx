"""
Python port of uri.ts, preserving equivalent behavior across Windows, macOS,
and Linux.

Notes on fidelity to the original TypeScript:
- `encode_uri_component` / `decode_uri_component` replicate JavaScript's
  encodeURIComponent / decodeURIComponent character sets exactly (the same
  set of characters is left unescaped).
- `urllib.parse.urlsplit` is used in place of the `uri-js` library's
  `URI.parse`. It is a pure string-based RFC 3986 style parser (no
  filesystem or OS calls), so it behaves identically regardless of platform.
- `urllib.parse.urljoin` is used in place of `URI.resolve`, implementing the
  same RFC 3986 section 5 reference-resolution algorithm.
- Path splitting/joining uses regex on literal "\\" / "/" characters rather
  than `os.path` or `pathlib`, so results do not vary between Windows and
  POSIX the way `os.path` would.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

# Characters encodeURIComponent leaves unescaped, beyond the RFC 3986
# "unreserved" set (letters, digits, - _ . ~) which Python's quote()
# already always treats as safe regardless of the `safe` argument.
_URI_COMPONENT_EXTRA_SAFE = "!*'()"


def encode_uri_component(value: str) -> str:
    """Equivalent to JavaScript's encodeURIComponent."""
    return quote(value, safe=_URI_COMPONENT_EXTRA_SAFE)


def decode_uri_component(value: str) -> str:
    """Equivalent to JavaScript's decodeURIComponent."""
    return unquote(value)


def path_to_uri_path_segment(path: str) -> str:
    """
    Converts any OS path to cleaned up URI path segment format with no
    leading/trailing slashes.
    e.g. \\path\\to\\folder\\ -> path/to/folder
         \\this\\is\\afile.ts -> this/is/afile.ts
         is/already/clean -> is/already/clean
    """
    clean = path.replace("\\", "/")  # backslashes -> forward slashes
    clean = re.sub(r"^/", "", clean)  # remove start slash
    clean = re.sub(r"/$", "", clean)  # remove end slash
    return "/".join(encode_uri_component(part) for part in clean.split("/"))


def get_clean_uri_path(uri: str) -> str:
    path = urlsplit(uri).path or ""
    clean = re.sub(r"^/", "", path)
    clean = re.sub(r"/$", "", clean)
    return clean


@dataclass
class FindUriInDirsResult:
    uri: str
    relative_path_or_basename: str
    found_in_dir: Optional[str]


def find_uri_in_dirs(
    uri: str,
    dir_uri_candidates: List[str],
) -> FindUriInDirsResult:
    uri_comps = urlsplit(uri)
    if not uri_comps.scheme:
        raise ValueError(f"Invalid uri: {uri}")

    uri_path_parts = get_clean_uri_path(uri).split("/")

    for dir_uri in dir_uri_candidates:
        dir_comps = urlsplit(dir_uri)

        if not dir_comps.scheme:
            raise ValueError(f"Invalid uri: {dir_uri}")

        if uri_comps.scheme != dir_comps.scheme:
            continue

        # Can't just use startswith because e.g.
        # file:///folder/file is not within file:///fold

        # At this point we break the path up and check if each dir path part matches
        dir_path_parts = get_clean_uri_path(dir_uri).split("/")

        if len(uri_path_parts) < len(dir_path_parts):
            continue

        all_dir_parts_match = True
        for i in range(len(dir_path_parts)):
            if dir_path_parts[i] != uri_path_parts[i]:
                all_dir_parts_match = False

        if all_dir_parts_match:
            relative_path = "/".join(
                decode_uri_component(part)
                for part in uri_path_parts[len(dir_path_parts):]
            )
            return FindUriInDirsResult(
                uri=uri,
                relative_path_or_basename=relative_path,
                found_in_dir=dir_uri,
            )

    # Not found
    return FindUriInDirsResult(
        uri=uri,
        relative_path_or_basename=get_uri_path_basename(uri),
        found_in_dir=None,
    )


def get_uri_path_basename(uri: str) -> str:
    """Returns just the file or folder name of a URI."""
    path = get_clean_uri_path(uri)
    parts = path.split("/")
    basename = parts[-1] if parts else ""
    return decode_uri_component(basename)


def get_file_extension_from_basename(basename: str) -> str:
    parts = basename.split(".")
    if len(parts) < 2:
        return ""
    return (parts[-1] or "").lower()


def get_uri_file_extension(uri: str) -> str:
    """Returns the file extension of a URI."""
    base_name = get_uri_path_basename(uri)
    return get_file_extension_from_basename(base_name)


def get_last_n_uri_relative_path_parts(
    dir_uri_candidates: List[str],
    uri: str,
    n: int,
) -> str:
    result = find_uri_in_dirs(uri, dir_uri_candidates)
    return get_last_n_path_parts(result.relative_path_or_basename, n)


def join_paths_to_uri(uri: str, *path_segments: str) -> str:
    base_uri = uri
    if not base_uri.endswith("/"):
        base_uri += "/"
    segments = [path_to_uri_path_segment(segment) for segment in path_segments]
    return urljoin(base_uri, "/".join(segments))


def join_encoded_uri_path_segment_to_uri(uri: str, path_segment: str) -> str:
    base_uri = uri
    if not base_uri.endswith("/"):
        base_uri += "/"
    return urljoin(base_uri, path_segment)


@dataclass
class UniqueUriPath:
    uri: str
    unique_path: str


def get_shortest_unique_relative_uri_paths(
    uris: List[str],
    dir_uri_candidates: List[str],
) -> List[UniqueUriPath]:
    # Split all URIs into segments and count occurrences of each suffix combination
    segment_combinations_map: Dict[str, int] = {}
    segments_info = []

    for uri in uris:
        relative_path_or_basename = find_uri_in_dirs(
            uri, dir_uri_candidates
        ).relative_path_or_basename
        segments = relative_path_or_basename.split("/")
        suffixes: List[str] = []

        # Generate all possible suffix combinations, starting from the
        # shortest (basename)
        for i in range(len(segments) - 1, -1, -1):
            suffix = "/".join(segments[i:])
            suffixes.append(suffix)  # ordered from shortest to longest
            segment_combinations_map[suffix] = (
                segment_combinations_map.get(suffix, 0) + 1
            )

        segments_info.append((uri, suffixes, relative_path_or_basename))

    # Find shortest unique path for each URI. Since suffixes are ordered
    # from shortest to longest, the first unique one we find is the shortest.
    result: List[UniqueUriPath] = []
    for uri, suffixes, relative_path_or_basename in segments_info:
        unique_path = next(
            (s for s in suffixes if segment_combinations_map.get(s) == 1),
            relative_path_or_basename,  # fallback to full path if none unique
        )
        result.append(UniqueUriPath(uri=uri, unique_path=unique_path))
    return result


def get_last_n_path_parts(filepath: str, n: int) -> str:
    """
    Only used when working with system paths and relative paths.
    Does not account for URI segments before the workspace.
    """
    if n <= 0:
        return ""
    parts = re.split(r"[\\/]", filepath)
    return "/".join(parts[-n:])


@dataclass
class UriDescription:
    uri: str
    relative_path_or_basename: str
    found_in_dir: Optional[str]
    last2_parts: str
    base_name: str
    extension: str


def get_uri_description(uri: str, dir_uri_candidates: List[str]) -> UriDescription:
    found = find_uri_in_dirs(uri, dir_uri_candidates)
    base_name = get_uri_path_basename(uri)
    extension = get_file_extension_from_basename(base_name)
    last2_parts = get_last_n_uri_relative_path_parts(dir_uri_candidates, uri, 2)
    return UriDescription(
        uri=uri,
        relative_path_or_basename=found.relative_path_or_basename,
        found_in_dir=found.found_in_dir,
        last2_parts=last2_parts,
        base_name=base_name,
        extension=extension,
    )
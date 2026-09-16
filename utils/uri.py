# utils/uri.py
from urllib.parse import quote, urljoin, unquote, urlparse, urlsplit
import os
import re
from typing import Optional, List
from dataclasses import dataclass
_WINDOWS = os.name == "nt"

def _encode_segment(segment: str) -> str:
    # backslashes -> forward slashes, strip surrounding slashes,
    # percent-encode each path component with the same safe set
    # as JS encodeURIComponent.
    clean = segment.replace("\\", "/").strip("/")
    return "/".join(quote(part, safe="!*'()") for part in clean.split("/"))


def join_paths_to_uri(uri: str, *path_segments: str) -> str:
    base = uri if uri.endswith("/") else uri + "/"
    segments = "/".join(_encode_segment(s) for s in path_segments)
    return urljoin(base, segments)


def get_uri_file_extension(filepath: str) -> str:
    """Return the lowercase file extension without the leading dot.

    Mirrors the behaviour of `getUriFileExtension` from `./uri` in the original
    module: the values returned are used directly as keys into
    `supportedLanguages`, which are dot-less and lowercase.
    """
    basename = os.path.basename(filepath)
    if "." not in basename:
        return ""
    return basename.rsplit(".", 1)[1].lower()
    

def get_file_extension_from_basename(basename: str) -> str:
    parts = basename.split(".")
    if len(parts) < 2:
        return ""
    return (parts[-1] or "").lower()


def get_clean_uri_path(uri: str) -> str:
    path = urlparse(uri).path or ""
    clean = path[1:] if path.startswith("/") else path  # remove start slash
    if clean.endswith("/"):
        clean = clean[:-1]  # remove end slash
    return clean


def get_uri_path_basename(uri: str) -> str:
    path = get_clean_uri_path(uri)
    basename = path.split("/")[-1] if path.split("/") else ""
    # `path.split("/")` always returns at least one element (possibly ""),
    # so `[-1]` matches JS `pop() || ""`.
    basename = basename or ""
    return unquote(basename)
    
    
def get_path_to_uri(path: str) -> str:
    p = os.path.abspath(path).replace(os.sep, "/")
    if _WINDOWS and not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p, safe="/:")


def get_uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"not a file:// uri: {uri!r}")
    p = unquote(parsed.path)
    if _WINDOWS and len(p) >= 3 and p[0] == "/" and p[2] == ":":
        p = p[1:]
    return p


# -------new--------


_URI_COMPONENT_EXTRA_SAFE = "!*'()"


def encode_uri_component(value: str) -> str:
    """Equivalent to JavaScript's encodeURIComponent."""
    return quote(value, safe=_URI_COMPONENT_EXTRA_SAFE)


def decode_uri_component(value: str) -> str:
    """Equivalent to JavaScript's decodeURIComponent."""
    return unquote(value)

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

def get_last_n_path_parts(filepath: str, n: int) -> str:
    """
    Only used when working with system paths and relative paths.
    Does not account for URI segments before the workspace.
    """
    if n <= 0:
        return ""
    parts = re.split(r"[\\/]", filepath)
    return "/".join(parts[-n:])


def get_last_n_uri_relative_path_parts(
    dir_uri_candidates: List[str],
    uri: str,
    n: int,
) -> str:
    result = find_uri_in_dirs(uri, dir_uri_candidates)
    return get_last_n_path_parts(result.relative_path_or_basename, n)


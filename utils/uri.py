# utils/uri.py
from urllib.parse import quote, urljoin, unquote, urlparse
import os

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
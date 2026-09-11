# utils/uri.py
from urllib.parse import quote, urljoin


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
# utils/ignore.py
import re
from typing import Iterable, List, Union

import pathspec


# --- default patterns ------------------------------------------------------
# One flat list. Security + indexing patterns + dirs, all merged.
# Everything is treated the same by the walker — no separate security layer.

default_ignore_file_and_dir: List[str] = [
    # --- files ---
    "*.env", "*.env.*", ".env*",
    "config.json", "config.yaml", "config.yml",
    "settings.json", "appsettings.json", "appsettings.*.json",
    "*.key", "*.pem", "*.p12", "*.pfx", "*.crt", "*.cer",
    "*.jks", "*.keystore", "*.truststore",
    "*.db", "*.sqlite", "*.sqlite3", "*.mdb", "*.accdb",
    "*.secret", "*.secrets", "auth.json", "*.token",
    "*.bak", "*.backup", "*.old", "*.orig",
    "docker-compose.override.yml", "docker-compose.override.yaml",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*.ppk", "*.gpg",

    "*.DS_Store", "*-lock.json", "*.lock", "*.log",
    "*.ttf", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4", "*.svg",
    "*.ico", "*.pdf", "*.zip", "*.gz", "*.tar", "*.dmg", "*.tgz",
    "*.rar", "*.7z", "*.exe", "*.dll", "*.obj", "*.o", "*.o.d",
    "*.a", "*.lib", "*.so", "*.dylib", "*.ncb", "*.sdf",
    "*.woff", "*.woff2", "*.eot", "*.cur",
    "*.avi", "*.mpg", "*.mpeg", "*.mov", "*.mp3", "*.mkv", "*.webm",
    "*.jar", "*.onnx", "*.parquet", "*.pqt", "*.wav", "*.webp",
    "*.wasm", "*.plist", "*.profraw", "*.gcda", "*.gcno",
    "go.sum", "*.gitignore", "*.gitkeep", "*.continueignore",
    "*.csv", "*.uasset", "*.pdb", "*.bin", "*.pag", "*.swp", "*.jsonl",

    # --- dirs ---
    ".env/", "env/",
    ".aws/", ".gcp/", ".azure/", ".kube/", ".docker/",
    "secrets/", ".secrets/", "private/", ".private/",
    "certs/", "certificates/", "keys/",
    ".ssh/", ".gnupg/", ".gpg/",
    "tmp/secrets/", "temp/secrets/", ".tmp/",

    ".git/", ".svn/", "node_modules/", "dist/", "build/", "Build/",
    "target/", "out/", "bin/", ".pytest_cache/", ".vscode-test/",
    "__pycache__/", "site-packages/", ".gradle/", ".mvn/", ".cache/",
    "gems/", "vendor/", ".venv/", "venv/",
    ".vscode/", ".idea/", ".vs/",
    ".continue/",
]


# --- Ignore wrapper (thin) -------------------------------------------------
class Ignore:
    """Chainable gitignore matcher, mirroring npm `ignore`'s used surface."""

    def __init__(self) -> None:
        self.patterns: List[str] = []
        self._spec = pathspec.PathSpec.from_lines("gitwildmatch", [])

    def add(self, patterns: Union[str, Iterable[str], "Ignore"]) -> "Ignore":
        if isinstance(patterns, Ignore):
            self.patterns.extend(patterns.patterns)
        elif isinstance(patterns, str):
            self.patterns.append(patterns)
        else:
            self.patterns.extend(patterns)
        self._spec = None
        return self

    def ignores(self, path: str) -> bool:
        if self._spec is None:
            self._spec = pathspec.PathSpec.from_lines("gitwildmatch", self.patterns)
        return self._spec.match_file(path.lstrip("/"))


# --- parser ----------------------------------------------------------------
_COMMENT_OR_EMPTY = re.compile(r"^#|^$")


def git_ig_array_from_file(file: str) -> List[str]:
    """Split a .gitignore-style file into a list of clean pattern strings."""
    return [
        line
        for raw in re.split(r"\r?\n", file)
        for line in [raw.strip()]
        if not _COMMENT_OR_EMPTY.match(line)
    ]
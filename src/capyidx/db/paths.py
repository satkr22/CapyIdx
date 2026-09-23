import os
from pathlib import Path
from typing import Callable, Awaitable, Union, Optional
import hashlib
from capyidx.base.index_d import BranchAndDir

DEFAULT_HOME_NAME = ".capyIdx"
DEFAULT_INDEX_DIR = ".codebase_index"

def home() -> Path:
    """Root data dir. Overridable with CAPYIDX_HOME."""
    env = os.environ.get("CAPYIDX_HOME")
    return Path(env).expanduser().resolve() if env else Path.home() / DEFAULT_HOME_NAME


def index_dir() -> Path:
    return home() / DEFAULT_INDEX_DIR


def db_path_for(tags: BranchAndDir) -> Path:
    """Deterministic per-(directory, branch) index path."""
    key = f"{tags.directory}__{tags.branch}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return index_dir() / f"index__{digest}.sqlite"


def metadata_path_for(tags: BranchAndDir) -> Path:
    return db_path_for(tags).with_suffix(".metadata.json")
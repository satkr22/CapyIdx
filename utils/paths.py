import os
from pathlib import Path
from typing import Callable, Awaitable, Union, Optional
import hashlib

def _resolve_coreIndexer_global_dir() -> Path:

    return Path.home() / ".coreIndexer"

COREINDEXER_GLOBAL_DIR: Path = _resolve_coreIndexer_global_dir()

DEFAULT_HOME_NAME = ".coreIndexer"
DEFAULT_INDEX_DIR = ".codebase_index"

def home() -> Path:
    """Root data dir. Overridable with COREINDEXER_HOME."""
    env = os.environ.get("COREINDEXER_HOME")
    return Path(env).expanduser().resolve() if env else Path.home() / DEFAULT_HOME_NAME


def index_dir() -> Path:
    return home() / DEFAULT_INDEX_DIR


def db_path_for(tags) -> Path:
    """Deterministic per-(directory, branch) index path."""
    key = f"{tags.directory}__{tags.branch}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return index_dir() / f"index__{digest}.sqlite"


def metadata_path_for(tags) -> Path:
    return db_path_for(tags).with_suffix(".metadata.json")





def get_coreIndexer_global_path() -> Path:
    coreIndexer_path = COREINDEXER_GLOBAL_DIR
    if not coreIndexer_path.exists():
        coreIndexer_path.mkdir(parents=True, exist_ok=True)
    return coreIndexer_path


def get_index_folder_path() -> Path:
    index_path = get_coreIndexer_global_path() / ".codebase_index"
    if not index_path.exists():
        index_path.mkdir(parents=True, exist_ok=True)
    return index_path

def get_migrations_folder_path() -> Path:
    migrations_path = get_coreIndexer_global_path() / ".migrations"
    if not migrations_path.exists():
        migrations_path.mkdir(parents=True, exist_ok=True) 
    return migrations_path


async def migrate(
    id: str,
    callback: Union[Callable[[], None], Callable[[], Awaitable[None]]],
    on_already_complete: Optional[Callable[[], None]] = None,
) -> None:
    if os.environ.get("NODE_ENV") == "test":
        result = callback()
        if result is not None and hasattr(result, "__await__"):
            await result
        return

    migrations_path = get_migrations_folder_path()
    migration_path = migrations_path / id

    if not migration_path.exists():
        try:
            print(f"Running migration: {id}")
            migration_path.write_text("") 
            result = callback()
            if result is not None and hasattr(result, "__await__"):
                await result
        except Exception as e:
            print(f"Migration {id} failed: {e}")
    elif on_already_complete is not None:
        on_already_complete()


def get_index_sqlite_path() -> Path:
    return get_index_folder_path() / "index.sqlite"


def get_lance_db_path() -> Path:
    return get_index_folder_path() / "lancedb"


def get_docs_sqlite_path() -> Path:
    return get_index_folder_path() / "docs.sqlite"
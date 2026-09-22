from typing import Callable, List, TypeVar
import subprocess
import os

from base.index_d import BranchAndDir, Chunk
from utils.parameters import RETRIEVAL_PARAMS, RERANK_DEFAULTS

T = TypeVar("T")


def deduplicate_array(array: List[T], equal: Callable[[T, T], bool]) -> List[T]:
    result: List[T] = []

    for item in array:
        if not any(equal(existing_item, item) for existing_item in result):
            result.append(item)

    return result


def deduplicate_chunks(chunks: List[Chunk]) -> List[Chunk]:
    return deduplicate_array(
        chunks,
        lambda a, b: (
            a.filepath == b.filepath
            and a.start_line == b.start_line
            and a.end_line == b.end_line
        ),
    )
    
    
def get_current_tags(directories: List[str]) -> List[BranchAndDir]:
    """Helper to build tags based on current git branch and workspace folders."""
    try:
        # Get current git branch
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], 
            text=True
        ).strip()
    except subprocess.CalledProcessError:
        branch = "NONE" # Fallback if not a git repo

    tags = []
    for directory in directories:
        tags.append(BranchAndDir(branch=branch, directory=directory))
        
    return tags


def rparam(name: str):
    try:
        return RETRIEVAL_PARAMS[name]
    except (KeyError, TypeError):
        return RERANK_DEFAULTS[name]
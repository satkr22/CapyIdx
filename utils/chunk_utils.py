import hashlib
from base.index_d import IndexTag

# Maximum length for table names to stay under OS filename limits
MAX_TABLE_NAME_LENGTH = 240

# Leave room for branch and artifact_id
MAX_DIR_LENGTH = 200


def tag_to_string(tag: IndexTag) -> str:
    result = f"{tag.directory}::{tag.branch}::{tag.artifact_id}"

    if len(result) <= MAX_TABLE_NAME_LENGTH:
        return result

    # Create a hash of the full directory path to ensure uniqueness
    dir_hash = hashlib.md5(tag.directory.encode("utf-8")).hexdigest()[:8]

    # Calculate how much space we have for the directory after accounting
    # for hash, separators, branch, and artifact_id
    non_dir_length = len(f"{dir_hash}_::{tag.branch}::{tag.artifact_id}")
    max_dir_for_truncated = MAX_TABLE_NAME_LENGTH - non_dir_length

    # Truncate from the beginning of directory path to preserve
    # the more unique end parts
    if len(tag.directory) > max_dir_for_truncated:
        truncated_dir = tag.directory[len(tag.directory) - max_dir_for_truncated:]
    else:
        truncated_dir = tag.directory

    return f"{dir_hash}_{truncated_dir}::{tag.branch}::{tag.artifact_id}"
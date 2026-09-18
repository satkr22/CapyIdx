"""Python conversion of CodeSnippetsIndex.ts.

Preserves the exact behaviour of the original TypeScript module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, List, Dict, Tuple

from tree_sitter import QueryCursor, Node, Query

import utils.tree_sitter as _utils_tree_sitter
from base.db import SqliteDB
from base.index_d import (
    FileSystem,
    IndexTag,
    IndexingProgressUpdate,
)
from base.index_types import (
    IndexResultType,
    MarkCompleteCallback,
    PathAndCacheKey,
    RefreshIndexResults,
    IndexContext,
    CodebaseIndexer
)
from utils.chunk_utils import tag_to_string
from utils.paths import migrate
from utils.tree_sitter import (
    get_full_language_name,
    get_parser_for_file,
    get_query_for_file,
)
from utils.uri import (
    find_uri_in_dirs,
    get_last_n_path_parts,
    get_last_n_uri_relative_path_parts,
    get_uri_path_basename,
)


# ---------------------------------------------------------------------------
# Local context types.
# ---------------------------------------------------------------------------


@dataclass
class ContextItem:
    name: str
    description: str
    content: str
    uri: dict


@dataclass
class ContextSubmenuItem:
    title: str
    description: str
    id: str


# ---------------------------------------------------------------------------
# Snippet chunk: `ChunkWithoutID & { title, signature }` from the TS module.
# ---------------------------------------------------------------------------


@dataclass
class SnippetChunk:
    title: str
    signature: str
    content: str
    start_line: int
    end_line: int


# Directory containing the `.scm` query files. The TypeScript module resolves
# these relative to the tree-sitter module's `__dirname`; we mirror that by
# resolving relative to `utils/tree_sitter.py`'s location.
_QUERY_BASE_DIR: Path = (
    Path(_utils_tree_sitter.__file__).resolve().parent
    / "tree_sitter_queries"
)


class CodeSnippetsCodebaseIndex(CodebaseIndexer):

    relative_expected_time: float = 1.0
    artifact_id: str = "codeSnippets"

    def __init__(self, filesystem: FileSystem, db: sqlite3.Connection) -> None:
        self.db = db
        self.db.row_factory = sqlite3.Row # safe to set again
        self.fs = filesystem

    # ------------------------------------------------------------------
    # Schema / migrations
    # ------------------------------------------------------------------

    @staticmethod
    async def _create_tables(db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS code_snippets (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                cacheKey TEXT NOT NULL,
                content TEXT NOT NULL,
                title TEXT NOT NULL,
                signature TEXT,
                startLine INTEGER NOT NULL,
                endLine INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS code_snippets_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                snippetId INTEGER NOT NULL,
                FOREIGN KEY (snippetId) REFERENCES code_snippets (id)
            )
            """
        )
        db.commit()

        # --- migration: add_signature_column ---------------------------
        async def _add_signature_column() -> None:
            table_info = db.execute("PRAGMA table_info(code_snippets)").fetchall()
            signature_column_exists = any(
                column["name"] == "signature" for column in table_info
            )
            if not signature_column_exists:
                db.execute("ALTER TABLE code_snippets ADD COLUMN signature TEXT")
                db.commit()

        await migrate("add_signature_column", _add_signature_column)

        # --- migration: delete_duplicate_code_snippets -----------------
        async def _delete_duplicate_code_snippets() -> None:
            # Delete duplicate entries in code_snippets
            db.execute(
                """
                DELETE FROM code_snippets
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM code_snippets
                    GROUP BY path, cacheKey, content, title, startLine, endLine
                )
                """
            )

            # Add unique constraint if it doesn't exist
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_code_snippets_unique
                ON code_snippets (path, cacheKey, content, title, startLine, endLine)
                """
            )

            # Delete code_snippets associated with duplicate
            # code_snippets_tags entries
            db.execute(
                """
                DELETE FROM code_snippets
                WHERE id IN (
                    SELECT snippetId
                    FROM code_snippets_tags
                    WHERE (snippetId, tag) IN (
                        SELECT snippetId, tag
                        FROM code_snippets_tags
                        GROUP BY snippetId, tag
                        HAVING COUNT(*) > 1
                    )
                )
                """
            )

            # Delete duplicate entries
            db.execute(
                """
                DELETE FROM code_snippets_tags
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM code_snippets_tags
                    GROUP BY snippetId, tag
                )
                """
            )

            # Add unique constraint if it doesn't exist
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_snippetId_tag
                ON code_snippets_tags (snippetId, tag)
                """
            )

            db.commit()

        await migrate(
            "delete_duplicate_code_snippets", _delete_duplicate_code_snippets
        )

    # ------------------------------------------------------------------
    # QueryMatch → SnippetChunk
    # ------------------------------------------------------------------

    @staticmethod
    def _capture_name(query: Any, capture: Any) -> str:
        """Capture name lookup supporting both tree_sitter Python APIs.

        Modern `tree_sitter` exposes `QueryCapture` objects with `.index`;
        some builds still surface `(index, node)` tuples.
        """
        index = capture[0] if isinstance(capture, tuple) else capture.index
        return query.capture_names[index]

    @staticmethod
    def _capture_node(capture: Any) -> Any:
        return capture[1] if isinstance(capture, tuple) else capture.node

    @staticmethod
    def _get_snippets_from_match(match: Tuple[int, Dict[str, List[Node]]]) -> SnippetChunk:
        """
        Convert a single tree-sitter match into a SnippetChunk.
        
        `match` is the structure returned by QueryCursor.matches():
            (pattern_index, {capture_name: [Node, ...]})
        """
        pattern_index, captures = match

        body_types_to_treat_as_signatures = {
            "interface_declaration",  # TypeScript, Java
            "struct_item",            # Rust
            "type_spec",              # Go
        }

        body_capture_group_prefixes = {"definition", "reference"}

        title = ""
        content = ""
        signature = ""
        start_line = 0
        end_line = 0
        has_seen_body = False

        # Flatten the captures dict into an ordered list of (name, node)
        # so the original left-to-right processing order is preserved.
        ordered_captures: List[Tuple[str, Node]] = []
        for name, nodes in captures.items():
            for node in nodes:
                ordered_captures.append((name, node))

        for name, node in ordered_captures:
            # "definition.function" → "definition"
            trimmed_capture_name = name.split(".", 1)[0]

            # node.text is bytes in py-tree-sitter
            node_text = node.text.decode("utf-8") if node.text else ""
            node_type = node.type

            if trimmed_capture_name in body_capture_group_prefixes:
                if node_type in body_types_to_treat_as_signatures:
                    # Override whatever was there before
                    signature = node_text
                    has_seen_body = True

                content = node_text
                start_line = node.start_point[0]   # row
                end_line = node.end_point[0]       # row
            else:
                if trimmed_capture_name == "name":
                    title = node_text

                if not has_seen_body:
                    signature += node_text + " "

                    if trimmed_capture_name == "comment":
                        signature += "\n"

        return SnippetChunk(
            title=title,
            content=content,
            signature=signature.strip(),
            start_line=start_line,
            end_line=end_line,
        )

    # ------------------------------------------------------------------
    # File → snippets
    # ------------------------------------------------------------------
    async def get_snippets_in_file(
        self, 
        filepath: str,
        contents: str,
    ) -> List[SnippetChunk]:
        """
        Parse a file and extract all code snippets defined by the
        corresponding .scm query.
        """
        parser = await get_parser_for_file(filepath)
        if parser is None:
            return []

        # py-tree-sitter expects bytes
        tree = parser.parse(contents.encode("utf-8"))

        language = get_full_language_name(filepath)
        if not language:
            return []

        query_path = _QUERY_BASE_DIR / f"{language.value}.scm"
        query: Optional[Query] = await get_query_for_file(filepath, query_path)
        if query is None:
            return []

        cursor = QueryCursor(query)
        matches = cursor.matches(tree.root_node)

        return [self._get_snippets_from_match(m) for m in matches]
    

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self,
        tag: IndexTag,
        context: IndexContext,
        results: RefreshIndexResults,
        mark_complete: MarkCompleteCallback,
    ) -> AsyncGenerator[IndexingProgressUpdate, Any]:
        
        db = self.db
        await CodeSnippetsCodebaseIndex._create_tables(db)
        tag_string = tag_to_string(tag)

        # --- Compute ---------------------------------------------------
        compute_count = len(results.compute)
        for i, compute in enumerate(results.compute):
            snippets: list[SnippetChunk] = []
            try:
                snippets = await self.get_snippets_in_file(
                    compute.path,
                    await self.fs.read_file(compute.path),
                )
            except Exception as e:
                # If can't parse, assume malformatted code
                pass

            # Add snippets to sqlite
            for snippet in snippets:
                cursor = db.execute(
                    "REPLACE INTO code_snippets "
                    "(path, cacheKey, content, title, signature, "
                    " startLine, endLine) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        compute.path,
                        compute.cache_key,
                        snippet.content,
                        snippet.title,
                        snippet.signature,
                        snippet.start_line,
                        snippet.end_line,
                    ),
                )
                last_id = cursor.lastrowid

                db.execute(
                    "REPLACE INTO code_snippets_tags (snippetId, tag) "
                    "VALUES (?, ?)",
                    (last_id, tag_string),
                )
                
            db.commit()

            yield IndexingProgressUpdate(
                desc=f"Indexing {get_uri_path_basename(compute.path)}",
                progress=i / compute_count if compute_count else 0.0,
                status="indexing",
            )
            await mark_complete([compute], IndexResultType.COMPUTE)

        # --- Delete ----------------------------------------------------
        for del_item in results.delete:
            rows = db.execute(
                "SELECT id FROM code_snippets WHERE path = ? AND cacheKey = ?",
                (del_item.path, del_item.cache_key),
            ).fetchall()

            if rows:
                snippet_ids = ",".join(str(row["id"]) for row in rows)

                db.execute(
                    f"DELETE FROM code_snippets WHERE id IN ({snippet_ids})"
                )

                db.execute(
                    "DELETE FROM code_snippets_tags "
                    f"WHERE snippetId IN ({snippet_ids})"
                )
                db.commit()

            await mark_complete([del_item], IndexResultType.DELETE)

        # --- Add tag ---------------------------------------------------
        for add_tag in results.add_tag:
            snippets: list[SnippetChunk] = []
            try:
                snippets = await self.get_snippets_in_file(
                    add_tag.path,
                    await self.fs.read_file(add_tag.path),
                )
            except Exception:
                # If can't parse, assume malformatted code
                pass

            for snippet in snippets:
                cursor = db.execute(
                    "REPLACE INTO code_snippets "
                    "(path, cacheKey, content, title, signature, "
                    " startLine, endLine) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        add_tag.path,
                        add_tag.cache_key,
                        snippet.content,
                        snippet.title,
                        snippet.signature,
                        snippet.start_line,
                        snippet.end_line,
                    ),
                )
                last_id = cursor.lastrowid

                db.execute(
                    "REPLACE INTO code_snippets_tags (snippetId, tag) "
                    "VALUES (?, ?)",
                    (last_id, tag_string),
                )
            db.commit()

            await mark_complete([add_tag], IndexResultType.ADD_TAG)

        # --- Remove tag ------------------------------------------------
        for remove_tag in results.remove_tag:
            rows = db.execute(
                "SELECT id FROM code_snippets "
                "WHERE cacheKey = ? AND path = ?",
                (remove_tag.cache_key, remove_tag.path),
            ).fetchall()

            if rows:
                snippet_ids = ",".join(str(row["id"]) for row in rows)
                db.execute(
                    "DELETE FROM code_snippets_tags "
                    "WHERE tag = ? "
                    f"  AND snippetId IN ({snippet_ids})",
                    (tag_string,),
                )
                db.commit()

            await mark_complete([remove_tag], IndexResultType.REMOVE_TAG)

    # ------------------------------------------------------------------
    # Static readers
    # ------------------------------------------------------------------

    @staticmethod
    async def get_for_id(id: int, workspace_dirs: list[str]) -> ContextItem:
        db = SqliteDB.get()
        row = db.execute(
            "SELECT * FROM code_snippets WHERE id = ?", (id,)
        ).fetchone()

        last_2_parts = get_last_n_uri_relative_path_parts(
            workspace_dirs, row["path"], 2
        )
        find_result = find_uri_in_dirs(row["path"], workspace_dirs)
        return ContextItem(
            name=row["title"],
            description=last_2_parts,
            content=(
                f"```{find_result.relative_path_or_basename}\n"
                f"{row['content']}\n```"
            ),
            uri={
                "type": "file",
                "value": row["path"],
            },
        )

    @staticmethod
    async def get_all(tag: IndexTag) -> list[ContextSubmenuItem]:
        db = SqliteDB.get()
        await CodeSnippetsCodebaseIndex._create_tables(db)
        try:
            rows = db.execute(
                """
                SELECT cs.id, cs.path, cs.title
                FROM code_snippets cs
                JOIN code_snippets_tags cst ON cs.id = cst.snippetId
                WHERE cst.tag = ?
                """,
                (tag_to_string(tag),),
            ).fetchall()

            return [
                ContextSubmenuItem(
                    title=row["title"],
                    description=get_last_n_path_parts(row["path"], 2),
                    id=str(row["id"]),
                )
                for row in rows
            ]
        except Exception as e:  # noqa: BLE001
            print(f"Error getting all code snippets: {e}")
            return []

    @staticmethod
    async def get_paths_and_signatures(
        workspace_dirs: list[str],
        uri_offset: int = 0,
        uri_batch_size: int = 100,
        snippet_offset: int = 0,
        snippet_batch_size: int = 100,
    ) -> dict:
        db = SqliteDB.get()
        await CodeSnippetsCodebaseIndex._create_tables(db)

        end_index = uri_offset + uri_batch_size
        uri_batch = workspace_dirs[uri_offset:end_index]

        like_patterns = [f"{dir_}%" for dir_ in uri_batch]
        if not like_patterns:
            return {
                "groupedByUri": {},
                "hasMoreUris": False,
                "hasMoreSnippets": False,
            }

        placeholders = " OR path LIKE ".join("?" for _ in like_patterns)

        query = f"""
            SELECT DISTINCT path, signature
            FROM code_snippets
            WHERE path LIKE {placeholders}
            ORDER BY path, signature
            LIMIT ? OFFSET ?
        """

        rows = db.execute(
            query,
            (*like_patterns, snippet_batch_size, snippet_offset),
        ).fetchall()

        grouped_by_uri: dict[str, list[str]] = {}

        for row in rows:
            path = row["path"]
            signature = row["signature"]
            grouped_by_uri.setdefault(path, []).append(signature)

        has_more_uris = end_index < len(workspace_dirs)
        has_more_snippets = len(rows) == snippet_batch_size

        return {
            "groupedByUri": grouped_by_uri,
            "hasMoreUris": has_more_uris,
            "hasMoreSnippets": has_more_snippets,
        }
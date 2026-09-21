"""Deterministic symbol lookup over the SQLite index.

The retrieval contract is deliberately small:

* find rows in ``symbols`` by name;
* use exact name matches, or case-insensitive substring matches if there are
  no exact matches;
* load chunks for an explicitly selected symbol;
* order those chunks by their piece chain and reconstruct their text;
* recursively materialize children for file and class symbols.

The module performs only direct table lookups and deterministic assembly.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional, Sequence, List

from .models import LookupResult, SymbolCode, SymbolMatch
from retrieval.utils import get_current_tags


@dataclass(frozen=True)
class _Scope:
    tags: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


class SymbolLookup:
    """Lookup and reconstruct symbols from an existing SQLite connection.

    ``db`` must contain the ``symbols`` and ``chunks`` tables documented by
    this project. Optional ``tags`` and ``filter_paths`` arguments restrict
    the same indexed data without changing match or ordering semantics.
    """

    def __init__(self, db: sqlite3.Connection, roots:List[str]) -> None:
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.tags = get_current_tags(roots)

    def find_symbols(
        self,
        name: str,
        *,
        tags: Optional[Sequence[Any]] = None,
        filter_paths: Optional[Sequence[str]] = None,
    ) -> list[SymbolMatch]:
        """Return unique matching symbol rows in deterministic order.

        Exact matching is a case-sensitive ``symbols.name = ?`` lookup. The
        fallback is a case-insensitive substring lookup and is used only when
        the exact lookup returns no rows. When multiple rows reconstruct to
        identical code, only the first deterministically ordered row remains.
        """

        if not name:
            return []

        scope = self._build_scope(self.tags, filter_paths)
        scope_sql, scope_params = self._symbol_scope_sql(scope)
        exact_rows = self.db.execute(
            f"""
            SELECT s.id, s.name, s.type, s.path, s.startLine, s.endLine
            FROM symbols s
            WHERE s.name = ? AND ({scope_sql})
            ORDER BY s.path, s.startLine, s.endLine, s.type, s.name, s.id
            """,
            [name, *scope_params],
        ).fetchall()
        if exact_rows:
            matches = [self._match_from_row(row, "exact") for row in exact_rows]
            return self._deduplicate_matches(matches, scope)
        
        case_insensitive_rows = self.db.execute(
            f"""
            SELECT s.id, s.name, s.type, s.path, s.startLine, s.endLine
            FROM symbols s
            WHERE s.name = ? COLLATE NOCASE AND ({scope_sql})
            ORDER BY s.path, s.startLine, s.endLine, s.type, s.name, s.id
            """,
            [name, *scope_params],
        ).fetchall()
        if case_insensitive_rows:
            matches = [self._match_from_row(row, "exact-case-insensitive") for row in case_insensitive_rows]
            return self._deduplicate_matches(matches, scope)

        substring_rows = self.db.execute(
            f"""
            SELECT s.id, s.name, s.type, s.path, s.startLine, s.endLine
            FROM symbols s
            WHERE instr(lower(s.name), lower(?)) > 0
              AND ({scope_sql})
            ORDER BY s.path, s.startLine, s.endLine, s.type, s.name, s.id
            """,
            [name, *scope_params],
        ).fetchall()
        matches = [self._match_from_row(row, "substring") for row in substring_rows]
        return self._deduplicate_matches(matches, scope)

    def _deduplicate_matches(
        self,
        matches: list[SymbolMatch],
        scope: _Scope,
    ) -> list[SymbolMatch]:
        """Keep one symbol row for each unique reconstructed code.

        The input list is already deterministically ordered. Therefore the
        first symbol that owns a given reconstructed text is retained and
        later aliases are removed. A container's key includes all descendant
        chunks, so a file symbol and a class symbol that expose the same code
        collapse to one result while distinct overloads remain separate.
        """

        unique: list[SymbolMatch] = []
        seen_code: set[str] = set()
        for match in matches:
            code = self.reconstruct(match.id, _scope=scope).reconstructed_text
            if code in seen_code:
                continue
            seen_code.add(code)
            unique.append(match)
        return unique

    def lookup(
        self,
        name: str,
        *,
        symbol_id: Optional[str] = None,
        selected_symbol_id: Optional[str] = None,
        tags: Optional[Sequence[Any]] = None,
        filter_paths: Optional[Sequence[str]] = None,
    ) -> LookupResult:
        """Find symbols and optionally reconstruct one selected symbol.

        When there is exactly one match it is selected automatically. With
        multiple matches, pass its ``symbol_id`` explicitly; no implicit
        best-match choice is made.
        """

        if symbol_id is not None and selected_symbol_id is not None:
            if str(symbol_id) != str(selected_symbol_id):
                raise ValueError("symbol_id and selected_symbol_id disagree")
        selected_id = symbol_id or selected_symbol_id

        matches = self.find_symbols(
            name,
            tags=self.tags,
            filter_paths=filter_paths,
        )
        if selected_id is None and len(matches) == 1:
            selected_id = matches[0].id

        selected = None
        if selected_id is not None:
            match = next((item for item in matches if item.id == str(selected_id)), None)
            if match is None:
                raise ValueError("symbol_id must identify one of the matching symbols")
            selected = self.reconstruct(
                match.id,
                tags=self.tags,
                filter_paths=filter_paths,
            )
        return LookupResult(query=name, matches=matches, selected=selected)

    def reconstruct(
        self,
        symbol_id: str,
        *,
        tags: Optional[Sequence[Any]] = None,
        filter_paths: Optional[Sequence[str]] = None,
        _scope: Optional[_Scope] = None,
    ) -> SymbolCode:
        """Reconstruct one symbol by id.

        A leaf includes only its own chunks. A file or class also contains a
        deterministic tree of all descendants, grouped under their parent.
        """

        scope = _scope or self._build_scope(self.tags, filter_paths)
        row = self._symbol_row(str(symbol_id), scope)
        if row is None:
            raise KeyError(f"symbol not found: {symbol_id}")

        rows_by_id = {str(row["id"]): row}
        if str(row["type"]).casefold() in {"file", "class"}:
            descendant_rows = self.db.execute(
                """
                WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM symbols WHERE id = ?
                    UNION ALL
                    SELECT child.id
                    FROM symbols child
                    JOIN descendants parent ON child.parentId = parent.id
                )
                SELECT s.id, s.type, s.name, s.parentId, s.startLine,
                       s.endLine, s.path, s.cacheKey
                FROM symbols s
                JOIN descendants d ON d.id = s.id
                WHERE s.id <> ?
                ORDER BY s.path, s.startLine, s.endLine, s.type, s.name, s.id
                """,
                [str(symbol_id), str(symbol_id)],
            ).fetchall()
            for child in descendant_rows:
                if self._row_in_scope(child, scope):
                    rows_by_id[str(child["id"])] = child

        children: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for child in rows_by_id.values():
            if str(child["id"]) == str(symbol_id):
                continue
            parent_id = child["parentId"]
            if parent_id is not None and str(parent_id) in rows_by_id:
                children[str(parent_id)].append(child)

        for child_rows in children.values():
            child_rows.sort(key=self._symbol_sort_key)

        return self._build_code_tree(row, children)

    async def retrieve(
        self,
        name: str,
        *,
        symbol_id: Optional[str] = None,
        selected_symbol_id: Optional[str] = None,
        tags: Optional[Sequence[Any]] = None,
        filter_paths: Optional[Sequence[str]] = None,
    ) -> LookupResult:
        """Async convenience wrapper for callers with an async pipeline."""

        return self.lookup(
            name,
            symbol_id=symbol_id,
            selected_symbol_id=selected_symbol_id,
            tags=self.tags,
            filter_paths=filter_paths,
        )

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    @staticmethod
    def _build_scope(
        tags: Optional[Sequence[Any]],
        filter_paths: Optional[Sequence[str]],
    ) -> _Scope:
        tag_values: list[str] = []
        for tag in tags or ():
            if isinstance(tag, str):
                tag_values.append(tag)
                continue
            if isinstance(tag, dict):
                direct = tag.get("tag")
                if direct is not None:
                    tag_values.append(str(direct))
                    continue
                directory = tag.get("directory", tag.get("dir"))
                branch = tag.get("branch")
            else:
                direct = getattr(tag, "tag", None)
                if direct is not None:
                    tag_values.append(str(direct))
                    continue
                directory = getattr(tag, "directory", getattr(tag, "dir", None))
                branch = getattr(tag, "branch", None)
            if directory is not None and branch is not None:
                tag_values.append(f"{directory}::{branch}::chunks")

        return _Scope(
            tags=tuple(dict.fromkeys(tag_values)),
            paths=tuple(dict.fromkeys(str(path) for path in filter_paths or ())),
        )

    @staticmethod
    def _symbol_sort_key(row: sqlite3.Row) -> tuple[Any, ...]:
        return (
            str(row["path"]),
            int(row["startLine"]),
            int(row["endLine"]),
            str(row["type"]),
            str(row["name"]),
            str(row["id"]),
        )

    def _symbol_scope_sql(self, scope: _Scope) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope.paths:
            marks = ",".join("?" for _ in scope.paths)
            clauses.append(f"s.path IN ({marks})")
            params.extend(scope.paths)
        if scope.tags:
            marks = ",".join("?" for _ in scope.tags)
            clauses.append(
                "EXISTS ("
                "SELECT 1 FROM chunks scoped_c "
                "JOIN chunk_tags scoped_ct ON scoped_ct.chunkId = scoped_c.id "
                "WHERE scoped_c.cacheKey = s.cacheKey "
                "AND scoped_c.path = s.path "
                f"AND scoped_ct.tag IN ({marks})"
                ")"
            )
            params.extend(scope.tags)
        return (" AND ".join(clauses) if clauses else "1 = 1"), params

    @staticmethod
    def _row_in_scope(row: sqlite3.Row, scope: _Scope) -> bool:
        return not scope.paths or str(row["path"]) in scope.paths

    def _symbol_row(self, symbol_id: str, scope: _Scope) -> Optional[sqlite3.Row]:
        scope_sql, scope_params = self._symbol_scope_sql(scope)
        return self.db.execute(
            f"""
            SELECT s.id, s.type, s.name, s.parentId, s.startLine,
                   s.endLine, s.path, s.cacheKey
            FROM symbols s
            WHERE s.id = ? AND ({scope_sql})
            """,
            [symbol_id, *scope_params],
        ).fetchone()

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------

    def _build_code_tree(
        self,
        row: sqlite3.Row,
        children: dict[str, list[sqlite3.Row]],
    ) -> SymbolCode:
        symbol_id = str(row["id"])
        piece_rows = self._ordered_chunks(symbol_id)
        child_codes = tuple(
            self._build_code_tree(child, children)
            for child in children.get(symbol_id, ())
        )
        return SymbolCode(
            id=symbol_id,
            name=str(row["name"]),
            type=str(row["type"]),
            path=str(row["path"]),
            start_line=int(row["startLine"]),
            end_line=int(row["endLine"]),
            code="".join(str(piece["content"]) for piece in piece_rows),
            children=child_codes,
            pieces=tuple(str(piece["content"]) for piece in piece_rows),
        )

    def _ordered_chunks(self, symbol_id: str) -> list[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT id, pieceIndex, pieceCount, prevChunk, nextChunk,
                   content, startLine, endLine, idx
            FROM chunks
            WHERE symbolId = ?
            """,
            [symbol_id],
        ).fetchall()
        if len(rows) < 2:
            return rows

        by_id = {str(row["id"]): row for row in rows}
        first = [row for row in rows if row["prevChunk"] is None]
        if len(first) != 1:
            return sorted(rows, key=self._piece_sort_key)

        ordered: list[sqlite3.Row] = []
        seen: set[str] = set()
        current: Optional[sqlite3.Row] = first[0]
        while current is not None:
            current_id = str(current["id"])
            if current_id in seen:
                return sorted(rows, key=self._piece_sort_key)
            seen.add(current_id)
            ordered.append(current)
            next_id = current["nextChunk"]
            current = by_id.get(str(next_id)) if next_id is not None else None

        if len(ordered) != len(rows):
            remaining = [row for row in rows if str(row["id"]) not in seen]
            ordered.extend(sorted(remaining, key=self._piece_sort_key))
        return ordered
    
    def _dedupe_key(self, symbol_id: str) -> str:
        row = self.db.execute(
            """SELECT path, startLine, endLine,
                    (SELECT COUNT(*) FROM chunks WHERE symbolId = ?) AS pieces
            FROM symbols WHERE id = ?""",
            (symbol_id, symbol_id),
        ).fetchone()
        return f"{row['path']}:{row['startLine']}:{row['endLine']}:{row['pieces']}"

    @staticmethod
    def _piece_sort_key(row: sqlite3.Row) -> tuple[Any, ...]:
        return (
            int(row["pieceIndex"]),
            int(row["pieceCount"]),
            int(row["startLine"]),
            int(row["endLine"]),
            int(row["idx"]),
            str(row["id"]),
        )

    @staticmethod
    def _match_from_row(row: sqlite3.Row, match_kind: str) -> SymbolMatch:
        return SymbolMatch(
            id=str(row["id"]),
            name=str(row["name"]),
            type=str(row["type"]),
            path=str(row["path"]),
            start_line=int(row["startLine"]),
            end_line=int(row["endLine"]),
            match_kind=match_kind,  # type: ignore[arg-type]
        )

__all__ = ["SymbolLookup"]

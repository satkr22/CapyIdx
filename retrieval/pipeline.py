"""Deterministic symbol lookup over the SQLite index.

The retrieval contract is deliberately small:

* find rows in ``symbols`` by name;
* use case-sensitive exact, case-insensitive exact, then case-insensitive
  substring matches, in that order;
* load chunks for an explicitly selected symbol;
* order those chunks by their piece chain and reconstruct their text;
* recursively materialize children for file and class symbols.

The module performs only direct table lookups and deterministic assembly.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence, List

from retrieval.models import LookupResult, SymbolCode, SymbolMatch
from retrieval.utils import get_current_tags



logger = logging.getLogger(__name__)


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
        self._ensure_chunk_piece_index()

    def _ensure_chunk_piece_index(self) -> None:
        """Best-effort index creation for the batched chunk lookup path."""

        try:
            exists = self.db.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'index' AND name = 'idx_chunks_symbol_piece'
                """
            ).fetchone()
            if exists is None:
                self.db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chunks_symbol_piece
                    ON chunks(symbolId, pieceIndex)
                    """
                )
        except sqlite3.Error:
            logger.warning(
                "Could not create optional idx_chunks_symbol_piece index",
                exc_info=True,
            )

    def find_symbols(
        self,
        name: str,
        *,
        tags: Optional[Sequence[Any]] = None,
        filter_paths: Optional[Sequence[str]] = None,
    ) -> list[SymbolMatch]:
        """Return unique matching symbol rows in deterministic order.

        Matching uses three tiers: case-sensitive exact name, then
        case-insensitive exact name, then case-insensitive substring. A later
        tier is used only when the previous tier returns no rows. When
        multiple rows expose the same indexed code range and piece count,
        only the first deterministically ordered row remains.
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
            matches = [self._match_from_row(row, "exact") for row in case_insensitive_rows]
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
        """Keep the first deterministically ordered row for each cheap key."""

        unique: list[SymbolMatch] = []
        seen_keys: set[tuple[str, int, int, int]] = set()
        for match in matches:
            key = self._dedupe_key(match.id)
            if key in seen_keys:
                continue
            unique.append(match)
            seen_keys.add(key)
        return unique

    def lookup(
        self,
        name: str,
        *,
        symbol_id: Optional[str] = None,
        selected_symbol_id: Optional[str] = None,
        tags: Optional[Sequence[Any]] = None,
        filter_paths: Optional[Sequence[str]] = None,
        detail: Literal["signature", "body"] = "body",
        include_children: bool = True,
        max_lines: int = 0,
    ) -> LookupResult:
        """Find symbols and optionally reconstruct one selected symbol.

        When there is exactly one match it is selected automatically. With
        multiple matches, pass its ``symbol_id`` explicitly; no implicit
        best-match choice is made.
        """

        self._validate_render_options(detail, max_lines)
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
            selected = self._reconstruct_for_lookup(
                match.id,
                tags=self.tags,
                filter_paths=filter_paths,
                detail=detail,
                include_children=include_children,
                max_lines=max_lines,
            )
        limitations = (
            "Name-based lookup returns definitions; callers require grep_search; "
            "no call-graph data.",
        ) if selected is not None else ()
        return LookupResult(
            query=name,
            matches=matches,
            selected=selected,
            limitations=limitations,
        )

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
        return self._reconstruct_for_scope(
            str(symbol_id),
            scope,
            detail="body",
            include_children=True,
            max_lines=0,
        )

    def _reconstruct_for_lookup(
        self,
        symbol_id: str,
        *,
        tags: Optional[Sequence[Any]],
        filter_paths: Optional[Sequence[str]],
        detail: Literal["signature", "body"],
        include_children: bool,
        max_lines: int,
    ) -> SymbolCode:
        # Keep the current lookup scoping behavior, including its use of the
        # instance's current tags.
        scope = self._build_scope(self.tags, filter_paths)
        return self._reconstruct_for_scope(
            str(symbol_id),
            scope,
            detail=detail,
            include_children=include_children,
            max_lines=max_lines,
        )

    def _reconstruct_for_scope(
        self,
        symbol_id: str,
        scope: _Scope,
        *,
        detail: Literal["signature", "body"],
        include_children: bool,
        max_lines: int,
    ) -> SymbolCode:
        row = self._symbol_row(str(symbol_id), scope)
        if row is None:
            raise KeyError(f"symbol not found: {symbol_id}")

        rows_by_id = {str(row["id"]): row}
        if include_children and str(row["type"]).casefold() in {"file", "class"}:
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

        code = self._build_code_tree(row, children, max_lines=max_lines)
        if detail == "signature":
            code = self._signature_tree(code, max_lines=max_lines)
        return code

    async def retrieve(
        self,
        name: str,
        *,
        symbol_id: Optional[str] = None,
        selected_symbol_id: Optional[str] = None,
        tags: Optional[Sequence[Any]] = None,
        filter_paths: Optional[Sequence[str]] = None,
        detail: Literal["signature", "body"] = "body",
        include_children: bool = True,
        max_lines: int = 0,
    ) -> LookupResult:
        """Async convenience wrapper for callers with an async pipeline."""

        return self.lookup(
            name,
            symbol_id=symbol_id,
            selected_symbol_id=selected_symbol_id,
            tags=self.tags,
            filter_paths=filter_paths,
            detail=detail,
            include_children=include_children,
            max_lines=max_lines,
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
        *,
        chunk_rows_by_symbol: Optional[dict[str, list[sqlite3.Row]]] = None,
        max_lines: int = 0,
    ) -> SymbolCode:
        if chunk_rows_by_symbol is None:
            symbol_ids: list[str] = []

            def collect_ids(current: sqlite3.Row) -> None:
                current_id = str(current["id"])
                symbol_ids.append(current_id)
                for child in children.get(current_id, ()):
                    collect_ids(child)

            collect_ids(row)
            chunk_rows_by_symbol = self._load_chunks_by_symbol(symbol_ids)

        symbol_id = str(row["id"])
        piece_rows = self._ordered_chunks_from_rows(
            chunk_rows_by_symbol.get(symbol_id, ())
        )
        piece_contents = tuple(str(piece["content"]) for piece in piece_rows)
        included_pieces, rendered_code = self._limit_pieces(
            piece_contents,
            max_lines,
        )
        signature_piece = next(
            (piece for piece in piece_rows if int(piece["pieceIndex"]) == 0),
            piece_rows[0] if piece_rows else None,
        )
        signature_value = (
            None
            if signature_piece is None or signature_piece["signature"] is None
            else str(signature_piece["signature"])
        )
        child_codes = tuple(
            self._build_code_tree(
                child,
                children,
                chunk_rows_by_symbol=chunk_rows_by_symbol,
                max_lines=max_lines,
            )
            for child in children.get(symbol_id, ())
        )
        return SymbolCode(
            id=symbol_id,
            name=str(row["name"]),
            type=str(row["type"]),
            path=str(row["path"]),
            start_line=int(row["startLine"]),
            end_line=int(row["endLine"]),
            code=rendered_code,
            signature=signature_value,
            children=child_codes,
            pieces=included_pieces,
        )

    def _ordered_chunks(self, symbol_id: str) -> list[sqlite3.Row]:
        rows_by_symbol = self._load_chunks_by_symbol([str(symbol_id)])
        return self._ordered_chunks_from_rows(rows_by_symbol.get(str(symbol_id), ()))

    def _load_chunks_by_symbol(
        self,
        symbol_ids: Sequence[str],
    ) -> dict[str, list[sqlite3.Row]]:
        ids = list(dict.fromkeys(str(symbol_id) for symbol_id in symbol_ids))
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"""
            SELECT id, symbolId, pieceIndex, pieceCount, prevChunk, nextChunk,
                   signature, content, startLine, endLine, idx
            FROM chunks
            WHERE symbolId IN ({marks})
            """,
            ids,
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[str(row["symbolId"])].append(row)
        return grouped

    def _ordered_chunks_from_rows(
        self,
        rows: Sequence[sqlite3.Row],
    ) -> list[sqlite3.Row]:
        rows = list(rows)
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

    @staticmethod
    def _limit_pieces(
        pieces: tuple[str, ...],
        max_lines: int,
    ) -> tuple[tuple[str, ...], str]:
        full_code = "".join(pieces)
        if max_lines <= 0 or len(pieces) == 0:
            return pieces, full_code

        included: list[str] = []
        for piece in pieces:
            candidate = "".join(included) + piece
            if len(candidate.splitlines()) > max_lines:
                omitted_lines = max(
                    0,
                    len(full_code.splitlines())
                    - len("".join(included).splitlines()),
                )
                sentinel = (
                    f"# ... {omitted_lines} more lines omitted, use "
                    'detail="signature" or read_file for the rest ...'
                )
                prefix = "".join(included)
                rendered = prefix + ("" if not prefix or prefix.endswith("\n") else "\n")
                return tuple(included), rendered + sentinel
            included.append(piece)

        return pieces, full_code

    @staticmethod
    def _signature_tree(
        code: SymbolCode,
        *,
        max_lines: int = 0,
        include_children: bool = True,
    ) -> SymbolCode:
        signature_pieces = () if code.signature is None else (code.signature,)
        _, rendered_signature = SymbolLookup._limit_pieces(
            signature_pieces,
            max_lines,
        )
        return SymbolCode(
            id=code.id,
            name=code.name,
            type=code.type,
            path=code.path,
            start_line=code.start_line,
            end_line=code.end_line,
            code=rendered_signature,
            signature=code.signature,
            children=(
                tuple(
                    SymbolLookup._signature_tree(
                        child,
                        max_lines=max_lines,
                        include_children=False,
                    )
                    for child in code.children
                )
                if include_children
                else ()
            ),
            pieces=(),
        )

    @staticmethod
    def _validate_render_options(
        detail: Literal["signature", "body"],
        max_lines: int,
    ) -> None:
        if detail not in {"signature", "body"}:
            raise ValueError('detail must be "signature" or "body"')
        if max_lines < 0:
            raise ValueError("max_lines must be non-negative")

    def _dedupe_key(self, symbol_id: str) -> tuple[str, int, int, int]:
        row = self.db.execute(
            """
            SELECT s.path, s.startLine, s.endLine,
                   (SELECT COUNT(*) FROM chunks WHERE symbolId = s.id) AS pieces
            FROM symbols s
            WHERE s.id = ?
            """,
            [symbol_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"symbol not found: {symbol_id}")
        return (
            str(row["path"]),
            int(row["startLine"]),
            int(row["endLine"]),
            int(row["pieces"]),
        )

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

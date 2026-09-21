"""Data returned by the deterministic symbol lookup tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


MatchKind = Literal["exact", "substring"]


@dataclass(frozen=True)
class SymbolMatch:
    """A row from ``symbols`` that matched the requested name."""

    id: str
    name: str
    type: str
    path: str
    start_line: int
    end_line: int
    match_kind: MatchKind

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "match_kind": self.match_kind,
        }


@dataclass(frozen=True)
class SymbolCode:
    """Reconstructed code for one symbol and, for containers, its children."""

    id: str
    name: str
    type: str
    path: str
    start_line: int
    end_line: int
    code: str
    children: tuple["SymbolCode", ...] = ()
    pieces: tuple[str, ...] = ()

    @property
    def reconstructed_text(self) -> str:
        """The complete text represented by this symbol's chunk tree."""

        return self.code + "".join(
            child.reconstructed_text for child in self.children
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "code": self.code,
            "pieces": list(self.pieces),
            "children": [child.as_dict() for child in self.children],
        }


@dataclass
class LookupResult:
    """The complete response for one symbol-name lookup."""

    query: str
    matches: list[SymbolMatch] = field(default_factory=list)
    selected: SymbolCode | None = None

    @property
    def match_kind(self) -> MatchKind | None:
        return self.matches[0].match_kind if self.matches else None

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "matches": [match.as_dict() for match in self.matches],
            "selected": self.selected.as_dict() if self.selected else None,
        }

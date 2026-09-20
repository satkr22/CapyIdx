# retrieval/base_pipeline.py
from abc import ABC
from collections import defaultdict
from typing import List
import os
import re
import sqlite3
import subprocess

from base.index_d import RetrieveConfig, BranchAndDir
from fts.fullTextSearchCodebaseIndex import FullTextSearchCodebaseIndex
from lance_db.lanceDbIndex import LanceDbIndex
from retrieval.models import Candidate, ContextItem
from embeddings.base import Embeddings
from utils.parameters import RETRIEVAL_PARAMS

# --------------------------------------------------------------------------
# Tunables. Any of these can be overridden by adding the key to RETRIEVAL_PARAMS.
# --------------------------------------------------------------------------
_DEFAULTS = {
    "rrfK": 60,                # RRF smoothing constant
    "vecGap": 0.20,            # drop vector-only hits this far below the best cosine sim
    "ftsKeep": 5,              # always keep the top-N BM25 hits regardless of vector score
    "anchorCount": 5,          # only expand context around the top-N ranked candidates
    "symbolAnchorLimit": 25,   # max chunks returned by symbol-name lookup
    "maxSiblingPieces": 6,     # max pieces pulled in per expanded symbol
    "siblingDiscount": 0.8,    # score multiplier for sibling pieces of an anchor
    "parentDiscount": 0.6,     # score multiplier for the parent's header chunk
    "pathPenalty": 0.5,        # score multiplier for tests/docs/readme paths
    "maxContainerLines": 150,  # nested-range dedupe: keep container only if <= this
}


def _param(name: str):
    try:
        return RETRIEVAL_PARAMS[name]
    except (KeyError, TypeError):
        return _DEFAULTS[name]


# Anchor tags written into Candidate.sources -> anchor strength
ANCHOR_SCORES = {"symbol_match": 1.0, "symbol_child": 0.8, "symbol_file": 0.5}

_NOISE_PATH = re.compile(
    r"(^|/)(tests?|docs?|examples?)/|(^|/)(test_?\w*|\w+_test)\.py$|(^|/)readme[^/]*$",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "is", "are", "was", "were", "how", "does", "do", "what", "why",
    "when", "where", "which", "it", "this", "that", "i", "you", "explain",
    "describe", "show", "tell", "me", "work", "works", "working", "use", "uses",
    "using", "used", "implement", "implemented", "implementation", "function",
    "method", "code", "can", "please", "about", "into", "from",
}


def _ph(items) -> str:
    return ",".join("?" for _ in items)


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def extract_identifiers(query: str) -> List[str]:
    """camelCase / PascalCase / snake_case / dotted tokens in a natural-language query."""
    out: List[str] = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_.]{2,}", query):
        tok = tok.strip(".")
        if len(tok) < 3:
            continue
        is_camel = any(c.islower() for c in tok) and any(c.isupper() for c in tok[1:])
        if "_" in tok or "." in tok or is_camel:
            out.append(tok)
    return list(dict.fromkeys(out))


def _identifier_variants(ident: str) -> List[str]:
    lower = ident.lower()
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", ident).lower()
    return list(dict.fromkeys([lower, snake]))


def anchor_score(c: Candidate) -> float:
    return max((ANCHOR_SCORES.get(s, 0.0) for s in (c.sources or [])), default=0.0)


class BaseRetrievalPipeline(ABC):
    def __init__(
        self,
        db: sqlite3.Connection,
        fts_index: FullTextSearchCodebaseIndex,
        lance_index: LanceDbIndex,
        embeddings_provider: Embeddings,
        root_directory: List[str],
    ):
        self.db = db  # expects db.row_factory = sqlite3.Row
        self.fts_index = fts_index
        self.lance_index = lance_index
        self.embeddings_provider = embeddings_provider
        self.root_directory = root_directory

    # ------------------------------------------------------------------
    # Stage 1+2: candidates (FTS + vector + symbol-name anchors), deduped
    # ------------------------------------------------------------------
    async def _fetch_candidates(
        self,
        query: str,
        tags: List[BranchAndDir],
        n: int = RETRIEVAL_PARAMS["nRetrieve"],
    ) -> List[Candidate]:
        if not tags:
            tags = self._build_default_tags()

        fts_config = RetrieveConfig(
            text=self._build_fts_query(query),
            n=n,
            tags=tags,
            filter_paths=None,
            bm25_threshold=RETRIEVAL_PARAMS["bm25Threshold"],
        )
        fts_results = await self.fts_index.retrieve(fts_config)

        # LanceDB embeds the query itself (the old extra embed() call was unused).
        lance_results = await self.lance_index.retrieve(
            query=query, n=n, tags=tags, filter_directory=None
        )

        merged: dict[str, Candidate] = {}

        for chunk, score, chunk_id in lance_results:
            merged[chunk_id] = Candidate(
                chunk_id=chunk_id,
                vector_score=score,  # cosine similarity, higher is better
                sources=["vector"],
                chunk_data=chunk,
            )

        for chunk, score, chunk_id in fts_results:  # bm25: NEGATIVE, lower is better
            if chunk_id in merged:
                merged[chunk_id].bm25_score = score
                merged[chunk_id].sources.append("fts")  # type: ignore
            else:
                merged[chunk_id] = Candidate(
                    chunk_id=chunk_id,
                    bm25_score=score,
                    sources=["fts"],
                    chunk_data=chunk,
                )

        # Symbol-name anchors: the query names an identifier -> look it up directly
        for chunk_id, tag in self._symbol_anchor_chunks(query, tags):
            if chunk_id in merged:
                if tag not in merged[chunk_id].sources:  # type: ignore
                    merged[chunk_id].sources.append(tag)  # type: ignore
            else:
                merged[chunk_id] = Candidate(chunk_id=chunk_id, sources=[tag])

        return list(merged.values())

    def _build_default_tags(self) -> list[BranchAndDir]:
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
            ).strip()
        except Exception:
            branch = "main"
        return [BranchAndDir(branch=branch, directory=os.path.abspath(os.getcwd()))]

    # ------------------------------------------------------------------
    # FTS query construction
    # ------------------------------------------------------------------
    def _build_fts_query(self, query: str) -> str:
        """Identifiers -> quoted phrases (substring match under the trigram tokenizer).
        No identifiers -> OR of quoted content words (generic words removed)."""
        idents = extract_identifiers(query)
        if idents:
            terms = [v for i in idents for v in _identifier_variants(i) if len(v) >= 3]
            return " OR ".join(_quote(t) for t in dict.fromkeys(terms))
        return self._build_nl_fts_query(query)

    def _build_nl_fts_query(self, query: str) -> str:
        words = re.findall(r"[a-z0-9_]+", query.lower())
        words = [w for w in words if len(w) >= 3 and w not in _STOP_WORDS]
        return " OR ".join(_quote(w) for w in dict.fromkeys(words))

    # ------------------------------------------------------------------
    # Symbol-name anchors (uses symbols table + parentId for children)
    # ------------------------------------------------------------------
    def _symbol_anchor_chunks(self, query: str, tags: List[BranchAndDir]) -> list[tuple[str, str]]:
        idents = extract_identifiers(query)
        if not idents:
            return []
        names = list({i.lower() for i in idents})
        ph = _ph(names)

        rows = self.db.execute(
            f"""
            SELECT c.id AS id, c.path AS path,
                   CASE WHEN lower(s.name) IN ({ph}) THEN 0 ELSE 1 END AS kind
            FROM chunks c
            JOIN symbols s ON s.id = c.symbolId
            LEFT JOIN symbols p ON p.id = s.parentId
            WHERE lower(s.name) IN ({ph}) OR lower(p.name) IN ({ph})
            ORDER BY kind, c.path, c.idx
            """,
            names * 3,
        ).fetchall()
        found = [(r["id"], "symbol_match" if r["kind"] == 0 else "symbol_child", r["path"]) for r in rows]

        if not found:  # fall back to file-stem match: chunkCodebaseIndex -> chunkCodebaseIndex.py
            for name in names:
                for r in self.db.execute(
                    "SELECT id, path FROM chunks WHERE lower(path) LIKE ? ORDER BY path, idx",
                    (f"%{name}.%",),
                ).fetchall():
                    found.append((r["id"], "symbol_file", r["path"]))

        # restrict to the tag directories (tag dirs may be file:// URIs)
        dirs = [
            t.directory.replace("file://", "", 1)
            for t in (tags or [])
            if getattr(t, "directory", None)
        ]
        if dirs:
            found = [f for f in found if any(f[2].startswith(d) for d in dirs)]

        return [(cid, tag) for cid, tag, _ in found[: _param("symbolAnchorLimit")]]

    # ------------------------------------------------------------------
    # Candidate pruning + fusion
    # ------------------------------------------------------------------
    def _prune_candidates(self, cands):
        vec = [c.vector_score for c in cands if c.vector_score is not None]
        top_vec = max(vec) if vec else None
        fts_ranked = sorted(
            (c for c in cands if c.bm25_score is not None),
            key=lambda c: c.bm25_score,
        )
        fts_keep = {c.chunk_id for c in fts_ranked[: _param("ftsKeep")]}

        # NEW: no meaningful vector signal -> don't prune on vector at all.
        # Keep everything that came from FTS or has an anchor; vector-only stays too.
        weak_signal = (top_vec is None) or (top_vec < 0.55)

        kept = []
        for c in cands:
            keep = (
                anchor_score(c) > 0
                or c.chunk_id in fts_keep
                or c.bm25_score is not None
                or weak_signal
                or (top_vec is not None
                    and c.vector_score is not None
                    and c.vector_score >= top_vec - _param("vecGap"))
            )
            if keep:
                kept.append(c)
        return kept

    def _fuse_rrf(self, cands: List[Candidate]) -> None:
        """Weighted reciprocal-rank fusion over vector / bm25 / symbol-anchor lists."""
        k = _param("rrfK")
        for c in cands:
            c.final_score = 0.0
        vec = sorted(
            (c for c in cands if c.vector_score is not None),
            key=lambda c: -(c.vector_score if c.vector_score is not None else float("-inf")),
        )
        fts = sorted(
            (c for c in cands if c.bm25_score is not None),
            key=lambda c: c.bm25_score if c.bm25_score is not None else float("inf"),
        )  # most negative first
        sym = sorted((c for c in cands if anchor_score(c) > 0), key=lambda c: -anchor_score(c))
        for lst, weight in ((vec, 1.0), (fts, 0.7), (sym, 2.0)):
            for rank, c in enumerate(lst, 1):
                c.final_score += weight / (k + rank)  # type: ignore

    def _fuse_weighted(self, cands, w_vec=0.65, w_bm25=0.35):
        bm = [c.bm25_score for c in cands if c.bm25_score is not None]
        best, worst = (min(bm), max(bm)) if bm else (0.0, 0.0)
        span = worst - best

        # Scale vector contribution so weak cosines don't dominate.
        vec_all = [c.vector_score for c in cands if c.vector_score is not None]
        top_vec = max(vec_all) if vec_all else 0.0
        # Squash: cosines below ~0.4 collapse toward 0
        def vsc(v):
            if v is None:
                return 0.0
            return max(0.0, (v - 0.30) / (top_vec - 0.30 + 1e-9)) * top_vec

        for c in cands:
            nb = ((worst - c.bm25_score) / span) if (span > 0 and c.bm25_score is not None) else \
                (1.0 if c.bm25_score is not None else 0.0)
            v = vsc(c.vector_score)
            base = w_vec * v + w_bm25 * nb
            # Both-source bonus
            if c.vector_score is not None and c.bm25_score is not None:
                base += 0.15
            c.normalized_bm25 = nb
            c.final_score = base + 0.5 * anchor_score(c)
    


    def _apply_path_penalty(self, cands: List[Candidate], query: str) -> None:
        if not cands or re.search(r"\b(tests?|readme|docs?|examples?)\b", query, re.IGNORECASE):
            return
        ids = [c.chunk_id for c in cands]
        paths = {
            r["id"]: r["path"]
            for r in self.db.execute(f"SELECT id, path FROM chunks WHERE id IN ({_ph(ids)})", ids)
        }
        pen = _param("pathPenalty")
        for c in cands:
            if _NOISE_PATH.search(paths.get(c.chunk_id, "")) and "symbol_match" not in (c.sources or []):
                c.final_score = (c.final_score or 0.0) * pen

    # ------------------------------------------------------------------
    # Stage 3: symbol expansion -- runs AFTER ranking, on the top anchors only.
    # Returns ONLY new context candidates; originals are never replaced.
    # ------------------------------------------------------------------
    async def _expand_via_symbols(
        self, anchors: List[Candidate], known_ids: set[str]
    ) -> List[Candidate]:
        if not anchors:
            return []

        ids = [a.chunk_id for a in anchors]
        chunk_info = {
            r["id"]: (r["symbolId"], r["pieceIndex"])
            for r in self.db.execute(
                f"SELECT id, symbolId, pieceIndex FROM chunks WHERE id IN ({_ph(ids)})", ids
            )
        }
        sym_ids = list({s for s, _ in chunk_info.values() if s})
        if not sym_ids:
            return []

        parent_of = {
            r["id"]: r["parentId"]
            for r in self.db.execute(
                f"SELECT id, parentId FROM symbols WHERE id IN ({_ph(sym_ids)})", sym_ids
            )
        }
        pieces: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for r in self.db.execute(
            f"SELECT id, symbolId, pieceIndex FROM chunks WHERE symbolId IN ({_ph(sym_ids)})",
            sym_ids,
        ):
            pieces[r["symbolId"]].append((r["pieceIndex"], r["id"]))

        parent_ids = list({p for p in parent_of.values() if p})
        header: dict[str, str] = {}  # parent symbol -> its first piece only (not the whole class)
        if parent_ids:
            for r in self.db.execute(
                f"SELECT id, symbolId FROM chunks WHERE symbolId IN ({_ph(parent_ids)}) AND pieceIndex = 0",
                parent_ids,
            ):
                header.setdefault(r["symbolId"], r["id"])

        sib_disc, par_disc, cap = _param("siblingDiscount"), _param("parentDiscount"), _param("maxSiblingPieces")
        new: dict[str, Candidate] = {}

        def add(chunk_id: str, score: float) -> None:
            if chunk_id in known_ids or chunk_id in new:
                return
            cand = Candidate(chunk_id=chunk_id, sources=["symbol_expansion"])
            cand.final_score = score
            new[chunk_id] = cand

        for a in anchors:
            sym, piece_idx = chunk_info.get(a.chunk_id, (None, 0))
            if not sym:
                continue
            base = a.final_score or 0.0
            near = sorted(pieces.get(sym, []), key=lambda p: abs(p[0] - piece_idx))[:cap]
            for _, cid in near:
                add(cid, base * sib_disc)
            h = header.get(parent_of.get(sym))  # type: ignore
            if h:
                add(h, base * par_disc)

        return list(new.values())

    # ------------------------------------------------------------------
    # Stage 5: materialise ContextItems (grouped per symbol)
    # ------------------------------------------------------------------
    def _materialize_context_items(self, candidates: List[Candidate]) -> List[ContextItem]:
        if not candidates:
            return []

        chunk_ids = [c.chunk_id for c in candidates]
        rows = self.db.execute(
            f"""
            SELECT c.id AS chunk_id, c.path, c.startLine, c.endLine, c.content,
                   c.symbolId, c.pieceIndex, c.pieceCount, c.idx,
                   s.id AS symbol_id, s.name AS symbol_name, s.type AS symbol_type
            FROM chunks c
            LEFT JOIN symbols s ON c.symbolId = s.id
            WHERE c.id IN ({_ph(chunk_ids)})
            """,
            chunk_ids,
        ).fetchall()
        row_map = {r["chunk_id"]: r for r in rows}

        groups: dict[str, list[Candidate]] = {}
        for cand in candidates:
            row = row_map.get(cand.chunk_id)
            if not row:
                continue
            groups.setdefault(row["symbol_id"] or cand.chunk_id, []).append(cand)

        items: List[ContextItem] = []
        for group in groups.values():
            group.sort(key=lambda c: row_map[c.chunk_id]["pieceIndex"])
            best = max(group, key=lambda c: c.final_score or 0.0)
            row = row_map[best.chunk_id]

            # Join pieces in order; mark gaps so a partial symbol isn't presented as contiguous.
            parts, prev = [], None
            for c in group:
                r = row_map[c.chunk_id]
                if prev is not None and r["pieceIndex"] != prev + 1:
                    parts.append("# ... (omitted pieces) ...")
                parts.append(r["content"])
                prev = r["pieceIndex"]

            sources = {s for c in group for s in (c.sources or [])}
            items.append(
                ContextItem(
                    chunk_id=best.chunk_id,
                    symbol_id=row["symbol_id"],
                    symbol_name=row["symbol_name"],
                    symbol_type=row["symbol_type"],
                    path=row["path"],
                    start_line=min(row_map[c.chunk_id]["startLine"] for c in group),
                    end_line=max(row_map[c.chunk_id]["endLine"] for c in group),
                    content="\n\n".join(parts),
                    score=best.final_score or 0.0,
                    source=list(sources),
                )
            )
        return items

    def _drop_nested(self, items: List[ContextItem]) -> List[ContextItem]:
        """If one item's line range contains another's (same file): keep the container when it
        is small, otherwise keep the focused child."""
        max_lines = _param("maxContainerLines")
        items = sorted(items, key=lambda i: i.score, reverse=True)
        protected = {id(i) for i in items if "symbol_match" in (i.source or [])}  # exact name hits are never dropped
        dropped: set[int] = set()
        for a in items:
            for b in items:
                if a is b or id(a) in dropped or id(b) in dropped or a.path != b.path:
                    continue
                if (a.start_line, a.end_line) == (b.start_line, b.end_line):
                    continue
                if a.start_line <= b.start_line and b.end_line <= a.end_line:  # a contains b
                    victim = b if (a.end_line - a.start_line + 1) <= max_lines else a
                    if id(victim) not in protected:
                        dropped.add(id(victim))
        return [i for i in items if id(i) not in dropped]
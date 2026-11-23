"""Character-order (字序) investigation over ordered old word lists.

Given one or more real, ordered :class:`~.marks.WordList` objects, we ask which
*strict total orders of the complete local type matrix* sort every word list
correctly.  The answer is expressed as **constraints**, not as a brute-force
listing of all permutations:

* each adjacent headword pair contributes one inequality ``sort(x) < sort(y)``
  when the first differing sort decides it;
* prefix violations and identical-headword / entry-number violations are
  *order-independent* contradictions and are reported directly.

The set of compatible total orders is then exactly the set of linear extensions
of that inequality DAG.  We enumerate them (bounded by ``limit``; the promised
pre-investigation precondition caps compatible total orders at ~20000) so they
can be fed to the binding side, but the reasoning is constraint-based.

A proposed order can be *checked* (:meth:`CollationInvestigation.check`), and a
mismatch is pinned to a concrete headword pair together with its edition
(block, segment and revision).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from ..errors import CollationError, MalformedMark, TooManyOrders
from .glyphs import GlyphOrder, TypeMatrix, entry_id_key
from .marks import WordList


@dataclass(frozen=True)
class GlyphConstraint:
    """``earlier`` must sort before ``later`` in any compatible glyph order."""

    earlier: str
    later: str
    source: str  # human-readable provenance, e.g. wordlist id + headword pair
    left_entry: Optional[str] = None
    right_entry: Optional[str] = None
    wordlist_id: Optional[str] = None

    def as_edge(self) -> tuple[str, str]:
        return (self.earlier, self.later)


@dataclass(frozen=True)
class ConstraintContradiction:
    """An order-independent contradiction inside one word list.

    ``kind`` is ``"prefix"`` (a longer headword precedes its own prefix) or
    ``"entry_number"`` (identical headwords out of entry-number order).
    """

    kind: str
    detail: str
    wordlist_id: str
    left_entry: str
    right_entry: str
    left_provenance: Mapping[str, object]
    right_provenance: Mapping[str, object]


@dataclass(frozen=True)
class CheckViolation:
    """Why a proposed order fails to sort some word list."""

    kind: str  # "glyph_order" | "prefix" | "entry_number"
    detail: str
    wordlist_id: str
    left_entry: str
    right_entry: str
    left_provenance: Mapping[str, object]
    right_provenance: Mapping[str, object]
    expected: Optional[str] = None
    actual: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "wordlist_id": self.wordlist_id,
            "left_entry": self.left_entry,
            "right_entry": self.right_entry,
            "left_provenance": dict(self.left_provenance),
            "right_provenance": dict(self.right_provenance),
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    order: GlyphOrder
    violations: tuple[CheckViolation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "order": list(self.order.order),
            "violations": [v.to_dict() for v in self.violations],
        }


@dataclass(frozen=True)
class InvestigationResult:
    matrix: TypeMatrix
    orders: tuple[GlyphOrder, ...]
    constraints: tuple[GlyphConstraint, ...]
    contradictions: tuple[ConstraintContradiction, ...]
    cycle: tuple[str, ...]
    truncated: bool = False

    @property
    def feasible(self) -> bool:
        return not self.contradictions and not self.cycle and len(self.orders) > 0

    @property
    def count(self) -> int:
        return len(self.orders)

    def to_dict(self) -> dict[str, object]:
        return {
            "matrix": list(self.matrix.glyphs),
            "feasible": self.feasible,
            "count": self.count,
            "orders": [list(o.order) for o in self.orders],
            "constraints": [
                {
                    "earlier": c.earlier,
                    "later": c.later,
                    "source": c.source,
                    "left_entry": c.left_entry,
                    "right_entry": c.right_entry,
                    "wordlist_id": c.wordlist_id,
                }
                for c in self.constraints
            ],
            "contradictions": [
                {
                    "kind": c.kind,
                    "detail": c.detail,
                    "wordlist_id": c.wordlist_id,
                    "left_entry": c.left_entry,
                    "right_entry": c.right_entry,
                    "left_provenance": dict(c.left_provenance),
                    "right_provenance": dict(c.right_provenance),
                }
                for c in self.contradictions
            ],
            "cycle": list(self.cycle),
            "truncated": self.truncated,
        }


def _provenance(headword) -> dict[str, object]:
    return {
        "entry_id": headword.entry_id,
        "text": headword.text,
        "block_id": headword.block_id,
        "segment_id": headword.segment_id,
        "revision": headword.revision,
        "interval": [headword.start, headword.end],
    }


def _find_cycle(nodes: Sequence[str], edges: set[tuple[str, str]]) -> tuple[str, ...]:
    """Return one directed cycle (as node list) or an empty tuple."""

    succ: dict[str, set[str]] = {n: set() for n in nodes}
    for a, b in edges:
        succ.setdefault(a, set()).add(b)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    stack: list[str] = []

    def visit(node: str) -> Optional[list[str]]:
        color[node] = GREY
        stack.append(node)
        for nxt in sorted(succ.get(node, ())):
            if color.get(nxt, WHITE) == GREY:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color.get(nxt, WHITE) == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for n in nodes:
        if color[n] == WHITE:
            found = visit(n)
            if found is not None:
                return tuple(found)
    return ()


def _linear_extensions(
    glyphs: Sequence[str],
    edges: set[tuple[str, str]],
    limit: int,
) -> tuple[list[tuple[str, ...]], bool]:
    """Enumerate topological orders of ``glyphs`` under ``edges``.

    Returns ``(orders, truncated)``.  Enumeration stops once ``limit + 1``
    orders have been produced; ``truncated`` then flags that more exist.
    """

    succ: dict[str, set[str]] = {g: set() for g in glyphs}
    indeg: dict[str, int] = {g: 0 for g in glyphs}
    for a, b in edges:
        if b not in succ[a]:
            succ[a].add(b)
            indeg[b] += 1

    results: list[tuple[str, ...]] = []
    current: list[str] = []
    nodes = sorted(glyphs, key=lambda g: (len(g), g))
    truncated = False

    def recurse(remaining: set[str]) -> None:
        nonlocal truncated
        if truncated:
            return
        if not remaining:
            results.append(tuple(current))
            if len(results) > limit:
                truncated = True
            return
        progressed = False
        for g in nodes:
            if g in remaining and indeg[g] == 0:
                progressed = True
                current.append(g)
                remaining.discard(g)
                for m in succ[g]:
                    indeg[m] -= 1
                recurse(remaining)
                for m in succ[g]:
                    indeg[m] += 1
                remaining.add(g)
                current.pop()
                if truncated:
                    return
        if not progressed and remaining:
            # A cycle among the remaining nodes: no linear extension.
            return

    recurse(set(glyphs))
    if truncated:
        # Drop the sentinel extra order.
        results = results[:limit]
    return results, truncated


class CollationInvestigation:
    """Collect old word lists / extra constraints, then solve or check."""

    def __init__(self, matrix: TypeMatrix) -> None:
        self.matrix = matrix
        self._wordlists: list[WordList] = []
        self._extra: list[GlyphConstraint] = []
        self._cache: Optional[InvestigationResult] = None

    # ------------------------------------------------------------------
    # inputs
    # ------------------------------------------------------------------
    def add_wordlist(self, wordlist: WordList) -> "CollationInvestigation":
        if tuple(wordlist.matrix.glyphs) != tuple(self.matrix.glyphs):
            raise MalformedMark(
                "word list was tokenised with a different type matrix than this "
                "investigation"
            )
        self._wordlists.append(wordlist)
        self._cache = None
        return self

    def add_constraint(
        self, earlier: str, later: str, *, source: str = "explicit"
    ) -> "CollationInvestigation":
        if earlier not in self.matrix.glyphs or later not in self.matrix.glyphs:
            raise MalformedMark(
                f"constraint {earlier!r}<{later!r} references a sort outside the matrix"
            )
        self._extra.append(
            GlyphConstraint(earlier=earlier, later=later, source=source)
        )
        self._cache = None
        return self

    @property
    def wordlists(self) -> tuple[WordList, ...]:
        return tuple(self._wordlists)

    # ------------------------------------------------------------------
    # solving
    # ------------------------------------------------------------------
    def _collect(self) -> tuple[list[GlyphConstraint], list[ConstraintContradiction]]:
        constraints: list[GlyphConstraint] = list(self._extra)
        contradictions: list[ConstraintContradiction] = []
        for wl in self._wordlists:
            hws = wl.headwords
            for left, right in zip(hws, hws[1:]):
                lt, rt = left.tokens, right.tokens
                # Find first differing sort index.
                k = 0
                limit = min(len(lt), len(rt))
                while k < limit and lt[k] == rt[k]:
                    k += 1
                if k == limit:
                    # One is a prefix of the other, or identical.
                    if len(lt) == len(rt):
                        # Identical headwords: must follow entry-number order.
                        if entry_id_key(left.entry_id) >= entry_id_key(right.entry_id):
                            contradictions.append(
                                ConstraintContradiction(
                                    kind="entry_number",
                                    detail=(
                                        f"word list {wl.wordlist_id!r} lists identical "
                                        f"headwords {left.entry_id!r} then "
                                        f"{right.entry_id!r}, but identical headwords "
                                        "must sort by stable entry number"
                                    ),
                                    wordlist_id=wl.wordlist_id,
                                    left_entry=left.entry_id,
                                    right_entry=right.entry_id,
                                    left_provenance=_provenance(left),
                                    right_provenance=_provenance(right),
                                )
                            )
                        continue
                    if len(lt) < len(rt):
                        # left is a proper prefix -> correctly first; no constraint.
                        continue
                    # right is a proper prefix of left -> must come first: contradiction.
                    contradictions.append(
                        ConstraintContradiction(
                            kind="prefix",
                            detail=(
                                f"word list {wl.wordlist_id!r} puts longer headword "
                                f"{left.entry_id!r} ({left.text!r}) before its own "
                                f"prefix {right.entry_id!r} ({right.text!r}); a short "
                                "prefix must sort before a longer word"
                            ),
                            wordlist_id=wl.wordlist_id,
                            left_entry=left.entry_id,
                            right_entry=right.entry_id,
                            left_provenance=_provenance(left),
                            right_provenance=_provenance(right),
                        )
                    )
                    continue
                ga, gb = lt[k], rt[k]
                if ga == gb:  # pragma: no cover - k already first diff
                    continue
                constraints.append(
                    GlyphConstraint(
                        earlier=ga,
                        later=gb,
                        source=(
                            f"word list {wl.wordlist_id!r}: headwords "
                            f"{left.entry_id!r}<{right.entry_id!r} at first differing "
                            f"sort {ga!r}/{gb!r}"
                        ),
                        left_entry=left.entry_id,
                        right_entry=right.entry_id,
                        wordlist_id=wl.wordlist_id,
                    )
                )
        return constraints, contradictions

    def solve(self, *, limit: int = 20000) -> InvestigationResult:
        constraints, contradictions = self._collect()
        edges: set[tuple[str, str]] = {c.as_edge() for c in constraints}
        glyphs = tuple(self.matrix.glyphs)

        # Self-loop / duplicate edges collapse; a self-loop is a contradiction.
        if any(a == b for a, b in edges):
            contradictions = list(contradictions)
        cycle = _find_cycle(glyphs, edges)

        orders: list[GlyphOrder] = []
        truncated = False
        if not contradictions and not cycle:
            raw, truncated = _linear_extensions(glyphs, edges, limit)
            if truncated:
                raise TooManyOrders(
                    "more compatible total orders than the configured limit "
                    f"({limit}); this is outside the promised investigation regime",
                    detail={"limit": limit},
                )
            orders = [
                GlyphOrder(order=tuple(o)) for o in raw
            ]
        result = InvestigationResult(
            matrix=self.matrix,
            orders=tuple(orders),
            constraints=tuple(constraints),
            contradictions=tuple(contradictions),
            cycle=cycle,
            truncated=truncated,
        )
        self._cache = result
        return result

    def result(self) -> InvestigationResult:
        if self._cache is None:
            return self.solve()
        return self._cache

    # ------------------------------------------------------------------
    # checking a proposed order
    # ------------------------------------------------------------------
    def check(self, order: Iterable[str]) -> CheckResult:
        proposed = tuple(order)
        if len(set(proposed)) != len(proposed):
            raise CollationError("proposed order repeats a sort")
        if set(proposed) != set(self.matrix.glyphs):
            missing = set(self.matrix.glyphs) - set(proposed)
            extra = set(proposed) - set(self.matrix.glyphs)
            raise CollationError(
                "proposed order must be a strict total order over the complete "
                f"type matrix; missing={sorted(missing)} extra={sorted(extra)}"
            )
        glyph_order = GlyphOrder(order=proposed)
        rank = glyph_order.rank
        violations: list[CheckViolation] = []

        for wl in self._wordlists:
            hws = wl.headwords
            for left, right in zip(hws, hws[1:]):
                lt, rt = left.tokens, right.tokens
                k = 0
                limit = min(len(lt), len(rt))
                while k < limit and lt[k] == rt[k]:
                    k += 1
                if k == limit:
                    if len(lt) == len(rt):
                        if entry_id_key(left.entry_id) >= entry_id_key(right.entry_id):
                            violations.append(
                                CheckViolation(
                                    kind="entry_number",
                                    detail=(
                                        f"identical headwords {left.entry_id!r} then "
                                        f"{right.entry_id!r} are out of entry-number "
                                        "order"
                                    ),
                                    wordlist_id=wl.wordlist_id,
                                    left_entry=left.entry_id,
                                    right_entry=right.entry_id,
                                    left_provenance=_provenance(left),
                                    right_provenance=_provenance(right),
                                )
                            )
                        continue
                    if len(lt) < len(rt):
                        continue
                    violations.append(
                        CheckViolation(
                            kind="prefix",
                            detail=(
                                f"longer headword {left.text!r} sorts before its "
                                f"prefix {right.text!r}, but a short prefix must come "
                                "first"
                            ),
                            wordlist_id=wl.wordlist_id,
                            left_entry=left.entry_id,
                            right_entry=right.entry_id,
                            left_provenance=_provenance(left),
                            right_provenance=_provenance(right),
                            expected=f"{right.entry_id} before {left.entry_id}",
                            actual=f"{left.entry_id} before {right.entry_id}",
                        )
                    )
                    continue
                ga, gb = lt[k], rt[k]
                ra, rb = rank[ga], rank[gb]
                if ra > rb:
                    violations.append(
                        CheckViolation(
                            kind="glyph_order",
                            detail=(
                                f"word list {wl.wordlist_id!r} needs sort {ga!r} "
                                f"before {gb!r} (headwords {left.entry_id!r} then "
                                f"{right.entry_id!r}), but the proposed order has "
                                f"rank({ga})={ra} > rank({gb})={rb}"
                            ),
                            wordlist_id=wl.wordlist_id,
                            left_entry=left.entry_id,
                            right_entry=right.entry_id,
                            left_provenance=_provenance(left),
                            right_provenance=_provenance(right),
                            expected=f"{ga} before {gb}",
                            actual=f"{gb} before {ga}",
                        )
                    )
        return CheckResult(ok=not violations, order=glyph_order, violations=tuple(violations))

"""New-book binding: fascicle/position queries under investigation + loose-page clues.

The new book is the actual set of entries (词条) to be bound.  Each fascicle
(帖) holds a fixed number of entries; the book is the entries sorted by the
still-unknown local character order, then packed consecutively into fascicles.
So a *complete glyph order* determines a *whole-book layout* (each entry's
fascicle and in-fascicle position).

Loose pages recovered from the bindery are physical clues:

* :class:`PositionClue`  -- entry E is in fascicle F at position P;
* :class:`AdjacentClue`  -- on the same fascicle, A is immediately before B.

**All loose pages belong to one book**, so every clue must hold simultaneously
under a single glyph order that also satisfies the investigation.

For a queried entry we report every (fascicle, position) that occurs in *some*
whole book compatible with the investigation and *every* clue, each with a
witness full-book glyph order so the binder can rebuild the sequence.  If the
position is unique the cataloguer may sign it in; otherwise all genuinely
possible positions are kept.  When there is no explanation at all we return a
minimal set of mutually conflicting clues (or the investigation contradiction).

This forward approach -- enumerate the surviving glyph orders, compute each full
layout, and eliminate by clue rank/adjacency -- never reasons about local
pairwise groups, so the classic pitfalls (a pair of sorts recurring under many
prefixes, locally orderable groups that cannot coexist globally) simply do not
arise: only complete, globally consistent orders survive.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from typing import Mapping, Optional, Sequence

from ..errors import InvalidBook, MalformedMark, UnknownEntry
from .glyphs import GlyphOrder, TypeMatrix, compare_headwords
from .investigation import CollationInvestigation, InvestigationResult
from .marks import Headword


@dataclass(frozen=True)
class BookEntry:
    entry_id: str
    tokens: tuple[str, ...]
    text: str
    block_id: str
    segment_id: str
    revision: int

    @classmethod
    def from_headword(cls, hw: Headword) -> "BookEntry":
        return cls(
            entry_id=hw.entry_id,
            tokens=hw.tokens,
            text=hw.text,
            block_id=hw.block_id,
            segment_id=hw.segment_id,
            revision=hw.revision,
        )


@dataclass(frozen=True)
class PositionClue:
    entry_id: str
    fascicle: int  # 1-based
    position: int  # 1-based within fascicle

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "position",
            "entry_id": self.entry_id,
            "fascicle": self.fascicle,
            "position": self.position,
        }


@dataclass(frozen=True)
class AdjacentClue:
    before_entry_id: str
    after_entry_id: str
    fascicle: Optional[int] = None  # 1-based, optional

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "adjacent",
            "before_entry_id": self.before_entry_id,
            "after_entry_id": self.after_entry_id,
            "fascicle": self.fascicle,
        }


Clue = object  # PositionClue | AdjacentClue


@dataclass(frozen=True)
class NewBook:
    book_id: str
    entries: tuple[BookEntry, ...]
    fascicle_capacity: int
    matrix: TypeMatrix

    def __post_init__(self) -> None:
        if not self.entries:
            raise InvalidBook("a new book must contain at least one entry")
        if self.fascicle_capacity < 1:
            raise InvalidBook("fascicle capacity must be >= 1")
        ids = [e.entry_id for e in self.entries]
        if len(set(ids)) != len(ids):
            raise InvalidBook("new book has duplicate entry numbers")
        for e in self.entries:
            if not all(self.matrix.contains(t) for t in e.tokens):
                raise MalformedMark(
                    f"entry {e.entry_id!r} uses a sort outside the type matrix"
                )

    @property
    def entry_ids(self) -> tuple[str, ...]:
        return tuple(e.entry_id for e in self.entries)

    @property
    def n_fascicles(self) -> int:
        n = len(self.entries)
        return (n + self.fascicle_capacity - 1) // self.fascicle_capacity

    def entry_map(self) -> dict[str, BookEntry]:
        return {e.entry_id: e for e in self.entries}

    @classmethod
    def from_headwords(
        cls,
        headwords: Sequence[Headword],
        fascicle_capacity: int,
        *,
        book_id: str = "book",
        matrix: Optional[TypeMatrix] = None,
    ) -> "NewBook":
        if not headwords:
            raise InvalidBook("a new book must contain at least one entry")
        if matrix is None:
            raise InvalidBook(
                "a type matrix is required to build a new book from headwords"
            )
        entries = tuple(BookEntry.from_headword(hw) for hw in headwords)
        return cls(
            book_id=book_id,
            entries=entries,
            fascicle_capacity=fascicle_capacity,
            matrix=matrix,
        )


@dataclass(frozen=True)
class QueryPossiblePosition:
    fascicle: int
    position: int
    witness_order: tuple[str, ...]
    book_order: tuple[str, ...]
    layout: Mapping[str, tuple[int, int]]

    def to_dict(self) -> dict[str, object]:
        return {
            "fascicle": self.fascicle,
            "position": self.position,
            "witness_order": list(self.witness_order),
            "book_order": list(self.book_order),
            "layout": {k: list(v) for k, v in self.layout.items()},
        }


@dataclass(frozen=True)
class ConflictExplanation:
    """Why there is no whole book: a minimal conflicting set of clues/constraints."""

    kind: str  # "investigation" | "loose_pages"
    detail: str
    conflicting_clues: tuple[Mapping[str, object], ...] = ()
    investigation: Optional[Mapping[str, object]] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "conflicting_clues": [dict(c) for c in self.conflicting_clues],
            "investigation": self.investigation,
        }


@dataclass(frozen=True)
class BookQueryResult:
    entry_id: str
    satisfiable: bool
    possible_positions: tuple[QueryPossiblePosition, ...]
    conflict: Optional[ConflictExplanation]
    compatible_book_count: int

    @property
    def unique(self) -> bool:
        return self.satisfiable and len(self.possible_positions) == 1

    @property
    def signed_position(self) -> Optional[tuple[int, int]]:
        if self.unique:
            p = self.possible_positions[0]
            return (p.fascicle, p.position)
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "satisfiable": self.satisfiable,
            "possible_positions": [p.to_dict() for p in self.possible_positions],
            "unique": self.unique,
            "signed_position": None
            if self.signed_position is None
            else list(self.signed_position),
            "compatible_book_count": self.compatible_book_count,
            "conflict": None if self.conflict is None else self.conflict.to_dict(),
        }


def _layout_for_order(
    book: NewBook, order: GlyphOrder
) -> tuple[tuple[str, ...], dict[str, tuple[int, int]]]:
    rank = order.rank
    entries = list(book.entries)

    def cmp(a: BookEntry, b: BookEntry) -> int:
        return compare_headwords(a.tokens, a.entry_id, b.tokens, b.entry_id, rank)

    entries.sort(key=cmp_to_key(cmp))
    k = book.fascicle_capacity
    layout: dict[str, tuple[int, int]] = {}
    ordered_ids: list[str] = []
    for r, e in enumerate(entries):
        fascicle = r // k + 1
        position = r % k + 1
        layout[e.entry_id] = (fascicle, position)
        ordered_ids.append(e.entry_id)
    return tuple(ordered_ids), layout


def _clue_holds(clue: Clue, layout: Mapping[str, tuple[int, int]]) -> bool:
    if isinstance(clue, PositionClue):
        return layout.get(clue.entry_id) == (clue.fascicle, clue.position)
    if isinstance(clue, AdjacentClue):
        a = layout.get(clue.before_entry_id)
        b = layout.get(clue.after_entry_id)
        if a is None or b is None:
            return False
        if a[0] != b[0]:  # different fascicles
            return False
        if b[1] != a[1] + 1:  # not immediately after
            return False
        if clue.fascicle is not None and a[0] != clue.fascicle:
            return False
        return True
    raise MalformedMark(f"unknown loose-page clue {clue!r}")


class BookSolver:
    """Bind a new book against an investigation and loose-page clues."""

    def __init__(
        self,
        book: NewBook,
        investigation: CollationInvestigation,
        *,
        limit: int = 20000,
    ) -> None:
        if tuple(investigation.matrix.glyphs) != tuple(book.matrix.glyphs):
            raise InvalidBook(
                "book and investigation use different type matrices"
            )
        self.book = book
        self.investigation = investigation
        self.limit = limit
        self._result: Optional[InvestigationResult] = None
        self._orders: Optional[tuple[GlyphOrder, ...]] = None
        self._layouts: Optional[
            list[tuple[GlyphOrder, tuple[str, ...], dict[str, tuple[int, int]]]]
        ] = None

    # ------------------------------------------------------------------
    def _investigation(self) -> InvestigationResult:
        if self._result is None:
            self._result = self.investigation.solve(limit=self.limit)
        return self._result

    def _compatible_orders(self) -> tuple[GlyphOrder, ...]:
        if self._orders is None:
            result = self._investigation()
            self._orders = result.orders
        return self._orders

    def _layout_cache(self) -> list[tuple[GlyphOrder, tuple[str, ...], dict[str, tuple[int, int]]]]:
        if self._layouts is None:
            self._layouts = [
                (order, *_layout_for_order(self.book, order))
                for order in self._compatible_orders()
            ]
        return self._layouts

    # ------------------------------------------------------------------
    def _validate_clues(self, clues: Sequence[Clue]) -> None:
        valid_ids = set(self.book.entry_ids)
        for clue in clues:
            if isinstance(clue, PositionClue):
                if clue.entry_id not in valid_ids:
                    raise UnknownEntry(
                        f"loose-page clue names entry {clue.entry_id!r} not in the new book",
                        detail={"entry_id": clue.entry_id},
                    )
                if clue.fascicle < 1 or clue.fascicle > self.book.n_fascicles:
                    raise InvalidBook(
                        f"fascicle {clue.fascicle} outside 1..{self.book.n_fascicles}"
                    )
                if clue.position < 1 or clue.position > self.book.fascicle_capacity:
                    raise InvalidBook(
                        f"position {clue.position} outside 1..{self.book.fascicle_capacity}"
                    )
            elif isinstance(clue, AdjacentClue):
                for eid in (clue.before_entry_id, clue.after_entry_id):
                    if eid not in valid_ids:
                        raise UnknownEntry(
                            f"loose-page clue names entry {eid!r} not in the new book",
                            detail={"entry_id": eid},
                        )
                if clue.before_entry_id == clue.after_entry_id:
                    raise MalformedMark("an adjacency clue needs two distinct entries")
                if clue.fascicle is not None and (
                    clue.fascicle < 1 or clue.fascicle > self.book.n_fascicles
                ):
                    raise InvalidBook(
                        f"fascicle {clue.fascicle} outside 1..{self.book.n_fascicles}"
                    )
            else:
                raise MalformedMark(f"unknown loose-page clue {clue!r}")

    # ------------------------------------------------------------------
    def surviving_layouts(
        self, clues: Sequence[Clue]
    ) -> list[tuple[GlyphOrder, tuple[str, ...], dict[str, tuple[int, int]]]]:
        """All (order, book_order, layout) compatible with every clue."""

        self._validate_clues(clues)
        survivors = []
        for order, ordered, layout in self._layout_cache():
            if all(_clue_holds(clue, layout) for clue in clues):
                survivors.append((order, ordered, layout))
        return survivors

    # ------------------------------------------------------------------
    def query(self, entry_id: str, clues: Sequence[Clue] = ()) -> BookQueryResult:
        if entry_id not in set(self.book.entry_ids):
            raise UnknownEntry(
                f"query names entry {entry_id!r} not in the new book",
                detail={"entry_id": entry_id},
            )
        inv = self._investigation()
        if inv.contradictions or inv.cycle or not inv.orders:
            conflict = ConflictExplanation(
                kind="investigation",
                detail=(
                    "the investigation admits no compatible glyph order, so no "
                    "whole book exists"
                ),
                investigation=inv.to_dict(),
            )
            return BookQueryResult(
                entry_id=entry_id,
                satisfiable=False,
                possible_positions=(),
                conflict=conflict,
                compatible_book_count=0,
            )

        survivors = self.surviving_layouts(clues)
        if not survivors:
            return BookQueryResult(
                entry_id=entry_id,
                satisfiable=False,
                possible_positions=(),
                conflict=self._conflict_explanation(clues),
                compatible_book_count=0,
            )

        # Deduplicate by (fascicle, position); keep a witness order per position.
        by_position: dict[tuple[int, int], QueryPossiblePosition] = {}
        for order, ordered, layout in survivors:
            fac, pos = layout[entry_id]
            key = (fac, pos)
            if key not in by_position:
                by_position[key] = QueryPossiblePosition(
                    fascicle=fac,
                    position=pos,
                    witness_order=order.order,
                    book_order=ordered,
                    layout=layout,
                )
        positions = tuple(by_position[k] for k in sorted(by_position))
        return BookQueryResult(
            entry_id=entry_id,
            satisfiable=True,
            possible_positions=positions,
            conflict=None,
            compatible_book_count=len(survivors),
        )

    # ------------------------------------------------------------------
    def _satisfiable(self, clues: Sequence[Clue]) -> bool:
        for _order, _ordered, layout in self._layout_cache():
            if all(_clue_holds(clue, layout) for clue in clues):
                return True
        return False

    def _conflict_explanation(self, clues: Sequence[Clue]) -> ConflictExplanation:
        # Greedy minimisation: shrink to a minimal still-unsatisfiable subset.
        core = list(clues)
        if not core:
            # No clues but no survivors means the investigation itself is empty,
            # which is handled earlier; guard anyway.
            return ConflictExplanation(
                kind="investigation",
                detail="no compatible whole book exists",
                investigation=self._investigation().to_dict(),
            )
        changed = True
        while changed:
            changed = False
            for i in range(len(core)):
                trial = core[:i] + core[i + 1 :]
                if trial and not self._satisfiable(trial):
                    core = trial
                    changed = True
                    break
        clue_dicts: list[Mapping[str, object]] = [c.to_dict() for c in core]
        return ConflictExplanation(
            kind="loose_pages",
            detail=(
                "the loose pages cannot all belong to one book: these "
                f"{len(core)} clue(s) are mutually conflicting (each pair may be "
                "individually consistent, but no single whole-book order satisfies "
                "them together)"
            ),
            conflicting_clues=tuple(clue_dicts),
        )

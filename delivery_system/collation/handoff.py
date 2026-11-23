"""Hand-off artifacts between the investigator and the bindery.

Two directions are supported, matching the brief:

**A. Investigator -> bindery (typesetting slip / 排字单).**
:func:`build_slip` records every *visible book order* compactly, grouped by the
shared prefix of the order (a prefix trie).  Before recording, glyph orders that
produce an identical book layout are pruned, so the slip never floods the binder
with layout-identical permutations.  The bindery can then independently match
loose pages against the slip (:func:`match_clues_against_slip`) without
re-running the investigation, and an outside worker holding only the slip can
rebuild every physical book sequence.

**B. Bindery -> investigator (position requirement -> feasibility certificate).**
:func:`certify_precedence` takes precedence relations that a fascicle position
demands and returns *feasible-order certificates* -- only full glyph orders that
actually satisfy them (or a conflict).  Local conclusions derived from loose
pages are checked with :func:`viable_conclusions`, so a newly found loose page can
:func:`revoke_on_new_clue` exactly those local conclusions it overturns while
leaving still-supported ones intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from ..errors import InvalidBook
from .book import (
    BookSolver,
    Clue,
    NewBook,
    _clue_holds,
    _layout_for_order,
)
from .glyphs import GlyphOrder
from .investigation import CollationInvestigation


# ----------------------------------------------------------------------
# A. Typesetting slip (investigator -> bindery)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class CollationSlip:
    """A compact, self-contained record of every distinct visible book order."""

    book_id: str
    entry_count: int
    fascicle_capacity: int
    prefix_trie: Mapping[str, object]
    distinct_book_orders: int
    pruned_layout_duplicates: int
    surviving_orders: int

    def to_dict(self) -> dict[str, object]:
        return {
            "book_id": self.book_id,
            "entry_count": self.entry_count,
            "fascicle_capacity": self.fascicle_capacity,
            "prefix_trie": self.prefix_trie,
            "distinct_book_orders": self.distinct_book_orders,
            "pruned_layout_duplicates": self.pruned_layout_duplicates,
            "surviving_orders": self.surviving_orders,
        }


def _trie_node() -> dict[str, object]:
    return {"witness_order": None, "children": {}}


def build_slip(
    book: NewBook,
    investigation: CollationInvestigation,
    clues: Sequence[Clue] = (),
    *,
    limit: int = 20000,
) -> CollationSlip:
    """Build a prefix-compressed slip of every visible book order.

    Glyph orders that yield an identical book layout collapse to one witness, so
    the slip is free of layout-identical duplicates that would only confuse the
    binder.
    """

    solver = BookSolver(book, investigation, limit=limit)
    survivors = solver.surviving_layouts(clues)

    by_book_order: dict[tuple[str, ...], GlyphOrder] = {}
    for order, ordered, _layout in survivors:
        by_book_order.setdefault(ordered, order)
    pruned = len(survivors) - len(by_book_order)

    root = _trie_node()
    for ordered, order in by_book_order.items():
        node = root
        for eid in ordered:
            children = node["children"]  # type: ignore[assignment]
            if eid not in children:
                children[eid] = _trie_node()
            node = children[eid]
        node["witness_order"] = list(order.order)

    return CollationSlip(
        book_id=book.book_id,
        entry_count=len(book.entries),
        fascicle_capacity=book.fascicle_capacity,
        prefix_trie=root,
        distinct_book_orders=len(by_book_order),
        pruned_layout_duplicates=pruned,
        surviving_orders=len(survivors),
    )


def _expand_trie(
    node: Mapping[str, object], prefix: list[str]
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    witness = node.get("witness_order")
    children = node.get("children") or {}
    if witness is not None:
        out.append({"book_order": list(prefix), "witness_order": list(witness)})
    for eid, child in children.items():  # type: ignore[union-attr]
        out.extend(_expand_trie(child, prefix + [eid]))
    return out


def reconstruct_book_orders(
    slip: Mapping[str, object] | CollationSlip,
) -> list[dict[str, object]]:
    """Expand a (possibly JSON-round-tripped) slip into full book orders.

    An outside worker with only the slip can rebuild every physical book
    sequence, each with a witness glyph order and the resulting layout.
    """

    data = slip.to_dict() if isinstance(slip, CollationSlip) else slip
    orders = _expand_trie(data["prefix_trie"], [])
    cap = int(data["fascicle_capacity"])
    for item in orders:
        item["layout"] = layout_from_book_order(item["book_order"], cap)
    return orders


def layout_from_book_order(
    book_order: Sequence[str], capacity: int
) -> dict[str, tuple[int, int]]:
    return {
        eid: (r // capacity + 1, r % capacity + 1)
        for r, eid in enumerate(book_order)
    }


def match_clues_against_slip(
    slip: Mapping[str, object] | CollationSlip, clues: Sequence[Clue]
) -> list[dict[str, object]]:
    """Bindery-side: independently filter a slip's book orders by loose pages."""

    cap = int(
        slip.fascicle_capacity
        if isinstance(slip, CollationSlip)
        else slip["fascicle_capacity"]
    )
    survivors: list[dict[str, object]] = []
    for item in reconstruct_book_orders(slip):
        layout = layout_from_book_order(item["book_order"], cap)
        if all(_clue_holds(clue, layout) for clue in clues):
            survivors.append(item)
    return survivors


# ----------------------------------------------------------------------
# B. Position requirement certificates (bindery -> investigator)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class PrecedenceCertificate:
    """A feasible glyph order that realizes a precedence requirement set."""

    glyph_order: tuple[str, ...]
    requirements: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PrecedenceResult:
    satisfiable: bool
    certificates: tuple[PrecedenceCertificate, ...]
    conflict: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "satisfiable": self.satisfiable,
            "certificates": [
                {"glyph_order": list(c.glyph_order), "requirements": [list(r) for r in c.requirements]}
                for c in self.certificates
            ],
            "conflict": self.conflict,
        }


def certify_precedence(
    book: NewBook,
    investigation: CollationInvestigation,
    requirements: Sequence[tuple[str, str]],
    *,
    limit: int = 20000,
) -> PrecedenceResult:
    """Given entry precedence ``(before, after)`` demands, return certificates.

    Each returned certificate is a full glyph order (字序凭单) that realizes every
    requirement simultaneously.  If none does, ``satisfiable`` is False and the
    conflict names the offending requirement.
    """

    solver = BookSolver(book, investigation, limit=limit)
    inv = solver._investigation()
    if inv.contradictions or inv.cycle or not inv.orders:
        return PrecedenceResult(
            satisfiable=False,
            certificates=(),
            conflict="the investigation admits no compatible glyph order",
        )
    valid = set(book.entry_ids)
    for before, after in requirements:
        for eid in (before, after):
            if eid not in valid:
                raise InvalidBook(f"precedence requirement names unknown entry {eid!r}")

    rejected: Optional[tuple[str, str]] = None
    satisfied: list[GlyphOrder] = []
    for order in solver._compatible_orders():
        _ordered, layout = _layout_for_order(book, order)
        bad = None
        for before, after in requirements:
            if not layout[before] < layout[after]:
                bad = (before, after)
                break
        if bad is None:
            satisfied.append(order)
        elif rejected is None:
            rejected = bad

    if not satisfied:
        req = rejected or (requirements[0] if requirements else ("?", "?"))
        return PrecedenceResult(
            satisfiable=False,
            certificates=(),
            conflict=(
                f"no compatible glyph order places entry {req[0]!r} before "
                f"{req[1]!r}; the requirement conflicts with the investigation"
            ),
        )
    req_tuple = tuple((b, a) for b, a in requirements)
    return PrecedenceResult(
        satisfiable=True,
        certificates=tuple(
            PrecedenceCertificate(glyph_order=order.order, requirements=req_tuple)
            for order in satisfied
        ),
    )


# ----------------------------------------------------------------------
# Revocation of local conclusions when a new loose page appears
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class LocalConclusion:
    """A *local* conclusion read off a subset of loose pages.

    Unlike a globally forced fact, a local conclusion was inferred from just the
    pages in ``depended_on`` (perhaps by eyeballing one fascicle's worth of
    fragments).  It may or may not be forced by all the evidence at once, which
    is precisely why a newly found page can overturn it.

    * ``kind="position"`` with ``target=(fascicle, position)``: entry
      ``entry_id`` sits at that exact place.
    * ``kind="precedence"``: entry ``entry_id`` precedes entry ``other_id``.
    """

    key: str
    kind: str
    entry_id: str
    target: Optional[tuple[int, int]] = None
    other_id: Optional[str] = None
    depended_on: tuple[Mapping[str, object], ...] = ()
    _clues: tuple[Clue, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "kind": self.kind,
            "entry_id": self.entry_id,
            "target": None if self.target is None else list(self.target),
            "other_id": self.other_id,
            "depended_on": [dict(c) for c in self.depended_on],
        }

    def holds(self, layout: Mapping[str, tuple[int, int]]) -> bool:
        if self.kind == "position":
            return layout.get(self.entry_id) == self.target
        if self.kind == "precedence":
            if self.other_id is None or self.entry_id not in layout or self.other_id not in layout:
                return False
            return layout[self.entry_id] < layout[self.other_id]
        return False


def viable_conclusions(
    book: NewBook,
    investigation: CollationInvestigation,
    conclusions: Sequence[LocalConclusion],
    clues: Sequence[Clue] = (),
    *,
    limit: int = 20000,
) -> list[LocalConclusion]:
    """Conclusions that hold in *every* whole book compatible with ``clues``."""

    solver = BookSolver(book, investigation, limit=limit)
    survivors = solver.surviving_layouts(clues)
    if not survivors:
        return []
    viable: list[LocalConclusion] = []
    for conclusion in conclusions:
        if all(conclusion.holds(layout) for _o, _b, layout in survivors):
            viable.append(conclusion)
    return viable


@dataclass(frozen=True)
class RevocationResult:
    retained: tuple[LocalConclusion, ...]
    revoked: tuple[LocalConclusion, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "retained": [c.to_dict() for c in self.retained],
            "revoked": [c.to_dict() for c in self.revoked],
        }


def revoke_on_new_clue(
    book: NewBook,
    investigation: CollationInvestigation,
    conclusions: Sequence[LocalConclusion],
    new_clue: Clue,
    *,
    limit: int = 20000,
) -> RevocationResult:
    """Re-derive each local conclusion with the new page; revoke the losers.

    A conclusion is re-checked against *its own* dependency pages plus the newly
    found page.  If the conclusion no longer holds in every whole book those
    pages allow, it is revoked; otherwise it is retained.  This withdraws exactly
    the local claims the new evidence overturns, while unrelated conclusions --
    backed by independent pages -- survive.
    """

    solver = BookSolver(book, investigation, limit=limit)
    retained: list[LocalConclusion] = []
    revoked: list[LocalConclusion] = []
    for conclusion in conclusions:
        base = list(conclusion._clues)
        clues = base + [new_clue]
        survivors = solver.surviving_layouts(clues)
        if survivors and all(conclusion.holds(layout) for _o, _b, layout in survivors):
            retained.append(conclusion)
        else:
            revoked.append(conclusion)
    return RevocationResult(retained=tuple(retained), revoked=tuple(revoked))

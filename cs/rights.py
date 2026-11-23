"""Rights clearance over the classification catalog.

Domain vocabulary (see ``materials/rights/README.md``):

* an **original** (原片) is a named image with a rectangular grid of cells
  (像格); a cell is ``(row, col)`` and has a stable row-major index;
* a **ticket** (工单) is an image under application; its *direct class* is the
  catalog node that carries its true provenance;
* a **term** (条款) is registered on a catalog node for one rights holder and
  fixes a use, the rectangles of the original they license, the attribution
  strings that must travel with the image, and the "protection groups" - cell
  groups that must never become fully public;
* **clearance** for one application + use resolves, per holder, the most
  specific applicable terms reachable *upward* from the direct class.

Rules implemented here (pure functions; the caller supplies a committed edge
set so the result is pinned to one cut):

1. Applicable nodes for a holder = nodes reachable upward from the direct
   class (the direct class itself included) carrying a term for that holder,
   original and use.
2. If a more specific applicable node exists, drop its strict ancestors.
3. Several remaining parallel nodes of one holder: licensed regions are
   united, attribution strings and protection groups all apply (attribution is
   de-duplicated - extra paths never add a second signature).
4. Different holders: licensed regions intersect, requirements are all kept.
5. Any registered holder without an applicable term holds the whole
   application (暂缓).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import catalog

Cell = tuple[int, int]
Rect = tuple[int, int, int, int]


# --------------------------------------------------------------- geometry


def normalize_rect(rect: Sequence[int]) -> Rect:
    """Return ``(top, left, bottom, right)`` with corners in order."""

    if len(rect) != 4:
        raise ValueError(f"rectangle must have four coordinates: {rect!r}")
    top, left, bottom, right = (int(v) for v in rect)
    return (min(top, bottom), min(left, right), max(top, bottom), max(left, right))


def rect_cells(rect: Sequence[int]) -> set[Cell]:
    top, left, bottom, right = normalize_rect(rect)
    return {
        (row, col)
        for row in range(top, bottom + 1)
        for col in range(left, right + 1)
    }


def cells_to_rects(cells: Iterable[Cell]) -> list[Rect]:
    """Greedily cover a cell set with rectangles (row runs, merged upward).

    The coverage is exact (no extra cells) and deterministic, so it is a
    faithful, compact witness of a licensed region that may not be a single
    rectangle after intersecting holders.
    """

    remaining = set(cells)
    rects: list[Rect] = []
    while remaining:
        top = min(remaining)
        # grow a horizontal run on this row
        row, left = top
        right = left
        while (row, right + 1) in remaining:
            right += 1
        bottom = row
        # extend the run downward while the full run is still present
        while all((bottom + 1, c) in remaining for c in range(left, right + 1)):
            bottom += 1
        for r in range(row, bottom + 1):
            for c in range(left, right + 1):
                remaining.discard((r, c))
        rects.append((row, left, bottom, right))
    return rects


def mirror_cell(cell: Cell, width: int) -> Cell:
    row, col = cell
    return (row, width - 1 - col)


def mirror_rect(rect: Sequence[int], width: int) -> Rect:
    top, left, bottom, right = normalize_rect(rect)
    return (top, width - 1 - right, bottom, width - 1 - left)


# ------------------------------------------------------------ term model


@dataclass(frozen=True)
class Term:
    """A holder's registered terms for one original + use on one node."""

    node_id: str
    holder: str
    original: str
    use: str
    rects: tuple[Rect, ...]
    attribution: tuple[str, ...]
    protections: tuple[tuple[Cell, ...], ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Term":
        return cls(
            node_id=raw["node"],
            holder=raw["holder"],
            original=raw["original"],
            use=raw["use"],
            rects=tuple(normalize_rect(r) for r in raw.get("rects", [])),
            attribution=tuple(raw.get("attribution", [])),
            protections=tuple(
                tuple(sorted((int(r), int(c)) for r, c in group))
                for group in raw.get("protections", [])
            ),
        )


# --------------------------------------------------------------- results


@dataclass
class HolderClearance:
    holder: str
    applicable_nodes: list[str]
    kept_nodes: list[str]
    dropped_ancestors: list[str]
    region: set[Cell]
    region_rects: list[Rect]
    attribution: list[str]
    protections: list[tuple[Cell, ...]]

    def region_witness(self) -> list[list[int]]:
        return [list(r) for r in self.region_rects]


@dataclass
class Clearance:
    ticket_id: str
    original: str
    use: str
    status: str  # "cleared" | "held"
    holders: list[HolderClearance] = field(default_factory=list)
    held_holders: list[str] = field(default_factory=list)
    region: set[Cell] = field(default_factory=set)
    region_rects: list[Rect] = field(default_factory=list)
    attribution: list[str] = field(default_factory=list)
    protections: list[tuple[Cell, ...]] = field(default_factory=list)

    @property
    def cleared(self) -> bool:
        return self.status == "cleared"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "original": self.original,
            "use": self.use,
            "status": self.status,
            "held_holders": list(self.held_holders),
            "region": [list(r) for r in self.region_rects],
            "region_cells": sorted([r, c] for r, c in self.region),
            "attribution": list(self.attribution),
            "protections": [
                sorted([r, c] for r, c in group) for group in self.protections
            ],
            "holders": [
                {
                    "holder": h.holder,
                    "applicable_nodes": list(h.applicable_nodes),
                    "kept_nodes": list(h.kept_nodes),
                    "dropped_ancestors": list(h.dropped_ancestors),
                    "region": h.region_witness(),
                    "region_cells": sorted([r, c] for r, c in h.region),
                    "attribution": list(h.attribution),
                    "protections": [
                        sorted([r, c] for r, c in group) for group in h.protections
                    ],
                }
                for h in self.holders
            ],
        }


# ----------------------------------------------------------- reduction


def _strict_ancestors(node: str, edges: Iterable[tuple[str, str]]) -> set[str]:
    return catalog.ancestors(node, edges, include_self=False)


def reduce_applicable(
    node_ids: Sequence[str], edges: Iterable[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    """Drop strict ancestors of any other applicable node.

    Returns ``(kept, dropped)``.  Kept nodes are mutually non-ancestral: within
    one holder's parallel branches each survives, but a node below another
    relevant node removes the upper one.
    """

    edge_set = tuple(edges)
    distinct = sorted(set(node_ids))
    dropped: set[str] = set()
    for node in distinct:
        for other in distinct:
            if other == node:
                continue
            if node in _strict_ancestors(other, edge_set):
                dropped.add(node)
                break
    kept = [n for n in distinct if n not in dropped]
    return kept, sorted(dropped)


def _unique(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def clear(
    *,
    ticket_id: str,
    direct_class: str,
    original: str,
    use: str,
    edges: Iterable[tuple[str, str]],
    terms: Sequence[Term],
    holders: Sequence[str],
) -> Clearance:
    """Resolve clearance for one application + use at one committed cut."""

    edge_set = tuple(edges)
    upward = catalog.ancestors(direct_class, edge_set, include_self=True)
    per_holder: list[HolderClearance] = []
    held: list[str] = []

    for holder in holders:
        applicable = [
            t
            for t in terms
            if t.holder == holder
            and t.original == original
            and t.use == use
            and t.node_id in upward
        ]
        kept_nodes, dropped = reduce_applicable(
            [t.node_id for t in applicable], edge_set
        )
        kept_set = set(kept_nodes)
        kept_terms = [t for t in applicable if t.node_id in kept_set]

        # one holder: union the licensed region over parallel kept nodes
        region: set[Cell] = set()
        attribution: list[str] = []
        protections: list[tuple[Cell, ...]] = []
        for term in kept_terms:
            for rect in term.rects:
                region |= rect_cells(rect)
            attribution.extend(term.attribution)
            protections.extend(term.protections)

        if not kept_terms:
            held.append(holder)
        per_holder.append(
            HolderClearance(
                holder=holder,
                applicable_nodes=sorted({t.node_id for t in applicable}),
                kept_nodes=kept_nodes,
                dropped_ancestors=sorted(d for d in dropped if d in upward),
                region=region,
                region_rects=cells_to_rects(region),
                attribution=_unique(attribution),
                protections=_dedupe_protections(protections),
            )
        )

    if held:
        return Clearance(
            ticket_id=ticket_id,
            original=original,
            use=use,
            status="held",
            holders=per_holder,
            held_holders=sorted(held),
        )

    # intersect holder regions, union requirements
    region = set().union(*(h.region for h in per_holder))
    for h in per_holder:
        region &= h.region
    attribution = _unique(a for h in per_holder for a in h.attribution)
    protections = _dedupe_protections(
        p for h in per_holder for p in h.protections
    )
    return Clearance(
        ticket_id=ticket_id,
        original=original,
        use=use,
        status="cleared",
        holders=per_holder,
        region=region,
        region_rects=cells_to_rects(region),
        attribution=attribution,
        protections=protections,
    )


def _dedupe_protections(
    groups: Iterable[tuple[Cell, ...]],
) -> list[tuple[Cell, ...]]:
    seen: set[frozenset[Cell]] = set()
    out: list[tuple[Cell, ...]] = []
    for group in groups:
        key = frozenset(group)
        if key and key not in seen:
            seen.add(key)
            out.append(tuple(sorted(group)))
    out.sort()
    return out

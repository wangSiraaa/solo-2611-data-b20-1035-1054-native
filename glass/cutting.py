"""Resumable guillotine cutting over physical stock.

A :class:`Piece` is a physical glass rectangle with an id, an extent expressed
in the coordinates of the sheet it came from, and a *physical* grain axis.
A cut is one straight line from one edge of a piece to the opposite edge,
splitting that piece into two; no kerf is modelled, and trimming scrap counts
as a cut.  A delivered pane must be a single piece: it is never joined from two
pieces and never contains a defect.

Grain
-----
A piece keeps its physical grain axis through every cut.  A pane is installed
with some quarter-turn ``turn``.  Turn 0 delivers the crop at its physical
width x height; turn 1 delivers it at height x width.  On the installation
front the grain axis of an odd turn is swapped.  Because an arrow reversal is
allowed, only the turn parity matters, so ``turn`` is always 0 or 1.

Handoff
-------
The shift may start from stock the previous shift already cut.  The
:class:`Ledger` records each leftover exactly as it now lies, plus the number
of cuts still allowed.  Planning continues only from those pieces; two halves
that were cut apart are never recombined, even if the untouched sheet could
have yielded the pane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from . import geom


class PlanError(Exception):
    """The request cannot be planned from the physical stock it is given."""


@dataclass(frozen=True)
class Defect:
    x: int
    y: int
    width: int
    height: int

    def overlaps(self, ox: int, oy: int, ow: int, oh: int) -> bool:
        return not (
            ox + ow <= self.x
            or self.x + self.width <= ox
            or oy + oh <= self.y
            or self.y + self.height <= oy
        )

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class Piece:
    id: str
    source: str
    x: int
    y: int
    width: int
    height: int
    grain: str
    color: str
    defects: tuple[Defect, ...] = ()

    @property
    def area(self) -> int:
        return self.width * self.height

    def rect(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "grain": self.grain,
            "color": self.color,
            "defects": [d.as_dict() for d in self.defects],
        }

    def clear(self, ox: int, oy: int, ow: int, oh: int) -> bool:
        if ox < self.x or oy < self.y:
            return False
        if ox + ow > self.x + self.width or oy + oh > self.y + self.height:
            return False
        return not any(d.overlaps(ox, oy, ow, oh) for d in self.defects)

    def copy(self) -> "Piece":
        return Piece(
            self.id, self.source, self.x, self.y, self.width, self.height,
            self.grain, self.color, tuple(self.defects),
        )


@dataclass(frozen=True)
class Cut:
    piece: str
    axis: str          # "vertical" splits x, "horizontal" splits y
    at: int            # absolute coordinate of the cut line
    kept: str          # id of the first (left/top) child
    offcut: str        # id of the second (right/bottom) child

    def as_dict(self) -> dict:
        return {
            "piece": self.piece,
            "axis": self.axis,
            "at": self.at,
            "kept": self.kept,
            "offcut": self.offcut,
        }


def _defects_in(
    defects: Sequence[Defect], x: int, y: int, width: int, height: int
) -> tuple[Defect, ...]:
    return tuple(d for d in defects if d.overlaps(x, y, width, height))


class Ledger:
    """Mutable physical stock plus the remaining cut budget."""

    def __init__(self, pieces: Sequence[Piece], remaining_cuts: int) -> None:
        if remaining_cuts < 0:
            raise PlanError("remaining_cuts cannot be negative")
        self.pieces: dict[str, Piece] = {}
        for piece in pieces:
            self.add(piece)
        self.max_cuts = remaining_cuts
        self.cuts: list[Cut] = []
        self._counter = 0

    def add(self, piece: Piece) -> None:
        if piece.id in self.pieces:
            raise PlanError(f"duplicate piece id {piece.id!r}")
        self.pieces[piece.id] = piece

    @property
    def remaining_cuts(self) -> int:
        return self.max_cuts - len(self.cuts)

    def copy(self) -> "Ledger":
        clone = Ledger([], self.max_cuts)
        clone.pieces = {pid: p.copy() for pid, p in self.pieces.items()}
        clone.cuts = list(self.cuts)
        clone._counter = self._counter
        return clone

    def _new_id(self, source: str) -> str:
        self._counter += 1
        return f"{source}#{self._counter}"

    # --------------------------------------------------------------- cutting
    def split(self, piece_id: str, axis: str, at: int) -> tuple[str, str]:
        """Cut one piece in two; return the (left/top, right/bottom) child ids.

        The left/top child keeps the original piece id, so the piece being
        worked on keeps a stable name; the other child gets a fresh id.  Both
        inherit grain, colour and the defects that overlap them.
        """

        if self.remaining_cuts <= 0:
            raise PlanError("cut budget exhausted")
        piece = self.pieces.get(piece_id)
        if piece is None:
            raise PlanError(f"unknown piece {piece_id!r}")
        if axis == "vertical":
            if not (piece.x < at < piece.x + piece.width):
                raise PlanError(
                    f"vertical cut at {at} outside piece {piece_id} "
                    f"[{piece.x},{piece.x + piece.width}]"
                )
            aw, bw = at - piece.x, piece.x + piece.width - at
            a = Piece(
                piece_id, piece.source, piece.x, piece.y,
                aw, piece.height, piece.grain, piece.color,
                _defects_in(piece.defects, piece.x, piece.y, aw, piece.height),
            )
            b = Piece(
                self._new_id(piece.source), piece.source, at, piece.y,
                bw, piece.height, piece.grain, piece.color,
                _defects_in(piece.defects, at, piece.y, bw, piece.height),
            )
        elif axis == "horizontal":
            if not (piece.y < at < piece.y + piece.height):
                raise PlanError(
                    f"horizontal cut at {at} outside piece {piece_id} "
                    f"[{piece.y},{piece.y + piece.height}]"
                )
            ah, bh = at - piece.y, piece.y + piece.height - at
            a = Piece(
                piece_id, piece.source, piece.x, piece.y,
                piece.width, ah, piece.grain, piece.color,
                _defects_in(piece.defects, piece.x, piece.y, piece.width, ah),
            )
            b = Piece(
                self._new_id(piece.source), piece.source, piece.x, at,
                piece.width, bh, piece.grain, piece.color,
                _defects_in(piece.defects, piece.x, at, piece.width, bh),
            )
        else:
            raise PlanError(f"unknown axis {axis!r}")
        self.pieces[piece_id] = a
        self.pieces[b.id] = b
        self.cuts.append(Cut(piece_id, axis, at, a.id, b.id))
        return a.id, b.id

    def peel(self, piece_id: str, x: int, y: int, width: int, height: int) -> str:
        """Guillotine-trim a piece down to the exact sub-rectangle.

        Trims left/right/top/bottom waste, one cut per waste strip.  Returns
        the id of the piece now exactly equal to the crop; the crop *stays*
        in the ledger until the caller removes it (a delivered pane must not
        be reused).  The cuts are all straight edge-to-edge cuts, so the order
        is a valid guillotine order.
        """

        cur = piece_id
        piece = self.pieces[cur]
        if not piece.clear(x, y, width, height):
            raise PlanError(
                f"crop ({x},{y},{width},{height}) is not clear inside {piece_id}"
            )
        if piece.x < x:
            a, b = self.split(cur, "vertical", x)   # a=left waste, b=crop side
            cur = b
        piece = self.pieces[cur]
        if piece.x + piece.width > x + width:
            a, b = self.split(cur, "vertical", x + width)  # a=crop side
            cur = a
        piece = self.pieces[cur]
        if piece.y < y:
            a, b = self.split(cur, "horizontal", y)  # a=top waste, b=crop side
            cur = b
        piece = self.pieces[cur]
        if piece.y + piece.height > y + height:
            a, b = self.split(cur, "horizontal", y + height)  # a=crop side
            cur = a
        final = self.pieces[cur]
        if final.rect() != (x, y, width, height):
            raise PlanError(
                f"internal peel error: got {final.rect()} expected "
                f"{(x, y, width, height)}"
            )
        return cur

    def take(self, piece_id: str) -> Piece:
        return self.pieces.pop(piece_id)


# --------------------------------------------------------------- placements


@dataclass
class PaneSpec:
    """One pane needed, with the sizes the signed measurements allow."""

    demand_id: str
    slot: str
    color: str
    grain: str | None                  # required grain on the install front
    sizes: list[tuple[int, int]]       # approved (width, height) on the front
    lap_for: Callable[[int, int], dict | None] | None = None

    def as_dict(self) -> dict:
        return {
            "demand_id": self.demand_id,
            "slot": self.slot,
            "color": self.color,
            "grain": self.grain,
            "sizes": [list(s) for s in self.sizes],
        }


@dataclass
class Placement:
    demand_id: str
    slot: str
    piece: str
    crop: tuple[int, int, int, int]    # x, y, width, height in the sheet frame
    size: tuple[int, int]              # install-front pane size
    turn: int                          # 0 or 1 quarter turns
    laps: dict | None

    def as_dict(self) -> dict:
        return {
            "demand": self.demand_id,
            "slot": self.slot,
            "piece": self.piece,
            "crop": list(self.crop),
            "size": list(self.size),
            "turn": self.turn,
            "laps": self.laps,
        }


def crop_options(spec: PaneSpec, piece: Piece):
    """Every (crop, install size, turn) way this pane can come from this piece."""

    if piece.color != spec.color:
        return []
    options = []
    for (w, h) in spec.sizes:
        for turn, cw, ch in ((0, w, h), (1, h, w)):
            if spec.grain is not None:
                installed = piece.grain if turn == 0 else geom.swap_axis(piece.grain)
                if installed != spec.grain:
                    continue
            if cw > piece.width or ch > piece.height:
                continue
            for ox in range(piece.x, piece.x + piece.width - cw + 1):
                for oy in range(piece.y, piece.y + piece.height - ch + 1):
                    if piece.clear(ox, oy, cw, ch):
                        options.append(((ox, oy, cw, ch), (w, h), turn))
    return options


def _waste(piece: Piece, crop: tuple[int, int, int, int]) -> int:
    return piece.area - crop[2] * crop[3]


@dataclass
class Plan:
    placements: list[Placement]
    cuts: list[Cut]
    remaining_cuts: int
    offcuts: list[Piece]

    def as_dict(self) -> dict:
        return {
            "placements": [p.as_dict() for p in self.placements],
            "cuts": [c.as_dict() for c in self.cuts],
            "cuts_used": len(self.cuts),
            "remaining_cuts": self.remaining_cuts,
            "offcuts": [p.as_dict() for p in self.offcuts],
        }


def plan_cuts(
    specs: Sequence[PaneSpec],
    ledger: Ledger,
    *,
    max_steps: int = 1_000_000,
    minimise_cuts: bool = False,
) -> Plan:
    """Assign every pane a piece and emit a resuming guillotine cut order.

    A depth-first search tries each demand against every viable
    (piece, crop, turn) option, least waste first.  Panes are peeled out of
    whole physical pieces and then removed from the stock, so the resulting
    order is a valid guillotine order that can be executed on the handed-over
    bench, and a delivered pane is never used again.  A branch that runs out
    of cut budget is pruned instead of aborting the whole search.

    With ``minimise_cuts`` the search is exhaustive within ``max_steps`` and
    returns the plan using the fewest cuts; otherwise it returns the first
    complete plan found (still ordered by least waste).
    """

    order = sorted(specs, key=lambda s: (len(s.sizes), s.demand_id))
    steps = [0]
    best: dict | None = None

    def better(cuts: int, leftover: int) -> bool:
        if best is None:
            return True
        if minimise_cuts:
            return (cuts, leftover) < best["key"]
        return False

    def search(index: int, ledger_now: Ledger, chosen: list[Placement]) -> None:
        nonlocal best
        steps[0] += 1
        if steps[0] > max_steps:
            raise PlanError("cut planning exceeded its search budget")
        if best is not None and not minimise_cuts:
            return
        if index == len(order):
            leftover = sum(p.area for p in ledger_now.pieces.values())
            key = (len(ledger_now.cuts), leftover)
            if better(*key):
                best = {
                    "key": key,
                    "placements": list(chosen),
                    "cuts": list(ledger_now.cuts),
                    "offcuts": [p.copy() for p in ledger_now.pieces.values()],
                    "remaining": ledger_now.remaining_cuts,
                }
            return

        spec = order[index]
        options = []
        for piece in ledger_now.pieces.values():
            for crop, size, turn in crop_options(spec, piece):
                options.append((_waste(piece, crop), piece.id, crop, size, turn))
        options.sort(key=lambda o: (o[0], o[1], o[2]))
        if not options:
            return
        for _waste_count, piece_id, crop, size, turn in options:
            child = ledger_now.copy()
            try:
                kept = child.peel(piece_id, *crop)
            except PlanError:
                continue  # this branch would overrun the cut budget
            child.take(kept)  # delivered; not available to later demands
            laps = spec.lap_for(*size) if spec.lap_for else None
            placement = Placement(
                spec.demand_id, spec.slot, piece_id, crop, size, turn, laps
            )
            search(index + 1, child, chosen + [placement])
            if best is not None and not minimise_cuts:
                return

    search(0, ledger.copy(), [])
    if best is None:
        raise PlanError(
            "no guillotine cut plan fits the handed-over stock within the "
            "remaining cut budget"
        )
    return Plan(
        placements=best["placements"],
        cuts=best["cuts"],
        remaining_cuts=best["remaining"],
        offcuts=best["offcuts"],
    )


# ------------------------------------------------------- resuming validation


def check_placements_within_pieces(
    placements: Sequence[Placement], pieces: Sequence[Piece]
) -> list[str]:
    """Flag any pane whose crop crosses a physical fracture line.

    After a hand-over the stock is a set of separate rectangles.  A crop may
    only be drawn from *one* of them.  A plan that spans the seam between two
    pieces would be gluing halves back together, which is forbidden even when
    the un-cut sheet could have produced the pane.
    """

    problems = []
    for placement in placements:
        x, y, w, h = placement.crop
        holders = [
            p.id for p in pieces if p.clear(x, y, w, h)
        ]
        if not holders:
            problems.append(
                f"{placement.demand_id}: crop {placement.crop} lies in no single "
                "handed-over piece"
            )
    return problems


def guillotine_order(
    pieces: Sequence[Piece],
    targets: Sequence[tuple[int, int, int, int]],
    *,
    max_depth: int = 128,
) -> list[tuple[str, str, int]] | None:
    """Find a straight-cut order that isolates every target rectangle.

    The spec warns that some full layouts have no guillotine order (a cyclic
    pinwheel of placements, for instance).  This is a constructive check: at
    each step it looks for a through-cut on one current rectangle that no
    wanted target straddles, applies it, and recurses.  Returns the cut list
    when every target ends up exactly as a piece, else ``None``.

    Because it is a search over cut choices, a ``None`` is a real
    "no guillotine order found", not a heuristic dead end that another order
    could avoid within the explored tree.
    """

    wanted = tuple(tuple(t) for t in targets)

    def recurse(
        work: list[Piece], cuts: list[tuple[str, str, int]], depth: int
    ) -> list[tuple[str, str, int]] | None:
        if all(any(w.rect() == t for w in work) for t in wanted):
            return cuts
        if depth >= max_depth:
            return None
        for idx, piece in enumerate(work):
            if piece.rect() in wanted:
                continue
            for axis in ("vertical", "horizontal"):
                lo = piece.x if axis == "vertical" else piece.y
                hi = lo + (piece.width if axis == "vertical" else piece.height)
                for at in range(lo + 1, hi):
                    if _target_straddles(piece, axis, at, wanted):
                        continue
                    child = [q.copy() for q in work]
                    _apply_cut(child, idx, axis, at)
                    result = recurse(
                        child, cuts + [(piece.id, axis, at)], depth + 1
                    )
                    if result is not None:
                        return result
        return None

    return recurse([p.copy() for p in pieces], [], 0)


def _target_straddles(
    piece: Piece,
    axis: str,
    at: int,
    wanted: Sequence[tuple[int, int, int, int]],
) -> bool:
    """True if cutting ``piece`` at ``at`` would slice through a wanted pane."""

    for (x, y, w, h) in wanted:
        if axis == "vertical":
            within_piece_rows = not (
                y + h <= piece.y or y >= piece.y + piece.height
            )
            if within_piece_rows and x < at < x + w:
                return True
        else:
            within_piece_cols = not (
                x + w <= piece.x or x >= piece.x + piece.width
            )
            if within_piece_cols and y < at < y + h:
                return True
    return False


def _apply_cut(work: list[Piece], idx: int, axis: str, at: int) -> tuple[Piece, Piece]:
    """Split ``work[idx]`` in place, appending the second part; return both."""

    piece = work[idx]
    second_id = f"{piece.id}/{len(work) + 1}"
    if axis == "vertical":
        a = Piece(piece.id, piece.source, piece.x, piece.y, at - piece.x,
                  piece.height, piece.grain, piece.color,
                  _defects_in(piece.defects, piece.x, piece.y, at - piece.x, piece.height))
        b = Piece(second_id, piece.source, at, piece.y,
                  piece.x + piece.width - at, piece.height, piece.grain, piece.color,
                  _defects_in(piece.defects, at, piece.y,
                              piece.x + piece.width - at, piece.height))
    else:
        a = Piece(piece.id, piece.source, piece.x, piece.y, piece.width,
                  at - piece.y, piece.grain, piece.color,
                  _defects_in(piece.defects, piece.x, piece.y, piece.width, at - piece.y))
        b = Piece(second_id, piece.source, piece.x, at,
                  piece.width, piece.y + piece.height - at, piece.grain, piece.color,
                  _defects_in(piece.defects, piece.x, at,
                              piece.width, piece.y + piece.height - at))
    work[idx] = a
    work.append(b)
    return a, b

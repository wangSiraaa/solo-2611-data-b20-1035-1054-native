"""Exhibition-label layout feasibility.

A **layout** (版式) is a panel of character cells.  It offers a finite,
already curator-ordered list of **compositions** (构图).  A composition prints
one crop of one named original (原片) onto the panel, optionally mirrored, and
records the panel cells actually printed.  Every image block needs its
attribution string (署名) run contiguously along one edge of the block, in whole
character cells, without covering the picture, leaving the panel, or colliding
with another signature.

A composition is *publishable* against a cumulative public record when

* its crop lies inside the licensed region for the application,
* its signature fits, and
* the union of already-public original cells and the cells this composition
  would print contains **no** protection group (禁止公开的像格组) in full.

Everything here is pure; committed state (the public record, the licence
region from :mod:`cs.rights`) is passed in, so a result is tied to one cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .rights import Cell, Rect, normalize_rect, rect_cells

Panel = tuple[int, int]
EDGES = ("top", "bottom", "left", "right")


@dataclass(frozen=True)
class Original:
    id: str
    name: str
    rows: int
    cols: int

    def contains(self, cell: Cell) -> bool:
        row, col = cell
        return 0 <= row < self.rows and 0 <= col < self.cols

    def all_cells(self) -> set[Cell]:
        return {(r, c) for r in range(self.rows) for c in range(self.cols)}

    def index(self, cell: Cell) -> int:
        return cell[0] * self.cols + cell[1]


@dataclass(frozen=True)
class Block:
    original: str
    rect: Rect
    flip: bool
    at: tuple[int, int]

    @property
    def height(self) -> int:
        return self.rect[2] - self.rect[0] + 1

    @property
    def width(self) -> int:
        return self.rect[3] - self.rect[1] + 1

    def crop_cells(self) -> set[Cell]:
        return rect_cells(self.rect)

    def picture_cells(self) -> set[Cell]:
        row0, col0 = self.at
        return {
            (row0 + dr, col0 + dc)
            for dr in range(self.height)
            for dc in range(self.width)
        }

    def cell_map(self) -> dict[Cell, Cell]:
        """panel cell -> original cell, honouring the horizontal flip."""

        top, left, _bottom, right = self.rect
        row0, col0 = self.at
        mapping: dict[Cell, Cell] = {}
        for dr in range(self.height):
            for dc in range(self.width):
                orig_col = left + (self.width - 1 - dc) if self.flip else left + dc
                mapping[(row0 + dr, col0 + dc)] = (top + dr, orig_col)
        return mapping

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Block":
        return cls(
            original=raw["original"],
            rect=normalize_rect(raw["rect"]),
            flip=bool(raw.get("flip", False)),
            at=(int(raw["at"][0]), int(raw["at"][1])),
        )


@dataclass(frozen=True)
class Composition:
    id: str
    blocks: tuple[Block, ...]
    note: str = ""

    def originals(self) -> set[str]:
        return {b.original for b in self.blocks}

    def crop_cells_by_original(self) -> dict[str, set[Cell]]:
        out: dict[str, set[Cell]] = {}
        for b in self.blocks:
            out.setdefault(b.original, set()).update(b.crop_cells())
        return out

    def printed_cells(self) -> set[Cell]:
        cells: set[Cell] = set()
        for b in self.blocks:
            cells |= b.picture_cells()
        return cells

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Composition":
        return cls(
            id=raw["id"],
            blocks=tuple(Block.from_dict(b) for b in raw["blocks"]),
            note=raw.get("note", ""),
        )


# ------------------------------------------------------- signature placement


def _edge_spans(
    block: Block, n: int, panel: Panel
) -> Iterable[tuple[str, tuple[Cell, ...]]]:
    """All contiguous signature spans of length ``n`` hugging one block edge."""

    rows, cols = panel
    at_r, at_c = block.at
    h, w = block.height, block.width
    if n == 0:
        yield ("none", ())
        return

    def in_panel(cells: Iterable[Cell]) -> bool:
        return all(0 <= r < rows and 0 <= c < cols for r, c in cells)

    if n <= w:
        for start in range(at_c, at_c + w - n + 1):
            cells = tuple((at_r - 1, c) for c in range(start, start + n))
            if in_panel(cells):
                yield ("top", cells)
        for start in range(at_c, at_c + w - n + 1):
            cells = tuple((at_r + h, c) for c in range(start, start + n))
            if in_panel(cells):
                yield ("bottom", cells)
    if n <= h:
        for start in range(at_r, at_r + h - n + 1):
            cells = tuple((r, at_c - 1) for r in range(start, start + n))
            if in_panel(cells):
                yield ("left", cells)
        for start in range(at_r, at_r + h - n + 1):
            cells = tuple((r, at_c + w) for r in range(start, start + n))
            if in_panel(cells):
                yield ("right", cells)


@dataclass
class Placement:
    block_index: int
    edge: str
    cells: tuple[Cell, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "block": self.block_index,
            "edge": self.edge,
            "cells": [list(c) for c in self.cells],
        }


@dataclass
class SignatureSolution:
    ok: bool
    placements: list[Placement] = field(default_factory=list)
    reason: str | None = None
    occupied: set[Cell] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "placements": [p.as_dict() for p in self.placements],
            "cells": sorted([r, c] for r, c in self.occupied),
        }


def place_signatures(
    comp: Composition,
    signature_lengths: Sequence[int],
    panel: Panel,
    *,
    reserved: Iterable[Cell] = (),
) -> SignatureSolution:
    """Find one signature placement per block, or explain why none exists.

    ``signature_lengths[i]`` is the number of character cells block ``i`` needs
    (equal to the length of its required attribution string).  Reserved cells
    (e.g. a fixed branch emblem / 馆徽) can hold neither picture nor signature.
    """

    reserved_set = set(reserved)
    pictures: list[set[Cell]] = []
    panel_cells = {(r, c) for r in range(panel[0]) for c in range(panel[1])}
    for index, block in enumerate(comp.blocks):
        pic = block.picture_cells()
        if not pic <= panel_cells:
            return SignatureSolution(False, reason=f"block {index} leaves the panel")
        if pic & reserved_set:
            return SignatureSolution(False, reason=f"block {index} covers a reserved cell")
        pictures.append(pic)
    for i in range(len(pictures)):
        for j in range(i + 1, len(pictures)):
            if pictures[i] & pictures[j]:
                return SignatureSolution(False, reason=f"blocks {i},{j} overlap")

    forbidden = set().union(*pictures) if pictures else set()
    forbidden |= reserved_set
    options: list[list[tuple[str, tuple[Cell, ...]]]] = []
    for index, block in enumerate(comp.blocks):
        n = signature_lengths[index] if index < len(signature_lengths) else 0
        spans = [
            (edge, cells)
            for edge, cells in _edge_spans(block, n, panel)
            if not (set(cells) & forbidden)
        ]
        if not spans and n > 0:
            return SignatureSolution(
                False,
                reason=f"no room for {n}-cell signature on block {index}",
            )
        options.append(spans)

    chosen: list[Placement] = []
    used: set[Cell] = set()

    def backtrack(index: int) -> bool:
        if index == len(options):
            return True
        for edge, cells in options[index]:
            cell_set = set(cells)
            if cell_set & used:
                continue
            used.update(cell_set)
            chosen.append(Placement(index, edge, cells))
            if backtrack(index + 1):
                return True
            chosen.pop()
            used.difference_update(cell_set)
        return False

    if backtrack(0):
        occupied = {c for p in chosen for c in p.cells}
        return SignatureSolution(True, placements=list(chosen), occupied=occupied)
    return SignatureSolution(False, reason="signatures collide")


# ----------------------------------------------------------- composition check


@dataclass
class CompositionCheck:
    composition_id: str
    feasible: bool
    reasons: list[str] = field(default_factory=list)
    printed: set[Cell] = field(default_factory=set)
    signature: SignatureSolution | None = None
    originals: set[str] = field(default_factory=set)
    cell_map: dict[Cell, Cell] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "composition": self.composition_id,
            "feasible": self.feasible,
            "reasons": list(self.reasons),
            "printed_cells": sorted([r, c] for r, c in self.printed),
            "signature": self.signature.as_dict() if self.signature else None,
            "cell_map": [
                {"panel": list(p), "original": list(o)}
                for p, o in sorted(self.cell_map.items())
            ],
        }


def _completes_any(
    public: set[Cell], addition: set[Cell], protections: Iterable[Iterable[Cell]]
) -> tuple[bool, tuple[Cell, ...] | None]:
    merged = public | addition
    for group in protections:
        group_set = set(group)
        if group_set and group_set <= merged:
            return True, tuple(sorted(group_set))
    return False, None


def check_composition(
    comp: Composition,
    *,
    panel: Panel,
    reserved: Iterable[Cell],
    signature_lengths: Sequence[int],
    license_region: set[Cell],
    public_cells: dict[str, set[Cell]],
    protections: dict[str, list[Iterable[Cell]]],
    original_ids: Iterable[str] | None = None,
) -> CompositionCheck:
    reasons: list[str] = []
    known = set(original_ids) if original_ids is not None else comp.originals()

    # licence: every crop cell must lie in the licensed region of its original
    crop = comp.crop_cells_by_original()
    for orig, cells in crop.items():
        if not cells <= license_region:
            missing = sorted(cells - license_region)
            reasons.append(
                f"unlicensed original cells for {orig}: {missing}"
            )

    signature = place_signatures(comp, signature_lengths, panel, reserved=reserved)
    if not signature.ok:
        reasons.append(signature.reason or "signature infeasible")

    if any(b.original not in known for b in comp.blocks):
        reasons.append("composition references an unknown original")

    # protection: per original, public + printed must not contain a full group
    printed: set[Cell] = set()
    for orig, cells in crop.items():
        printed |= cells
        done, group = _completes_any(
            public_cells.get(orig, set()), cells, protections.get(orig, [])
        )
        if done:
            reasons.append(
                f"would complete protection group on {orig}: {list(group)}"
            )

    cell_map: dict[Cell, Cell] = {}
    for b in comp.blocks:
        cell_map.update(b.cell_map())

    return CompositionCheck(
        composition_id=comp.id,
        feasible=not reasons,
        reasons=reasons,
        printed=printed,
        signature=signature,
        originals=set(crop),
        cell_map=cell_map,
    )


# --------------------------------------------------------------- batch solver


@dataclass
class Label:
    id: str
    application_no: int
    original: str
    compositions: list[Composition]
    signature_length: int
    license_region: set[Cell]
    panel: Panel
    reserved: set[Cell] = field(default_factory=set)
    branch: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Label":
        return cls(
            id=raw["id"],
            application_no=int(raw["application_no"]),
            original=raw["original"],
            compositions=[Composition.from_dict(c) for c in raw["compositions"]],
            signature_length=int(raw.get("signature_length", 0)),
            license_region={tuple(c) for c in raw.get("license_region", [])},
            panel=(int(raw["panel"][0]), int(raw["panel"][1])),
            reserved={tuple(c) for c in raw.get("reserved", [])},
            branch=raw.get("branch", ""),
        )


@dataclass
class SolvedLabel:
    label_id: str
    application_no: int
    composition: Composition
    check: CompositionCheck

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label_id,
            "application_no": self.application_no,
            "composition": self.composition.id,
            "check": self.check.as_dict(),
        }


@dataclass
class BatchResult:
    status: str  # "delivered" | "blocked"
    order: list[str] = field(default_factory=list)
    solved: list[SolvedLabel] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    public_after: dict[str, list[Cell]] = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        return self.status == "delivered"


def solve_batch(
    labels: Sequence[Label],
    *,
    initial_public: dict[str, set[Cell]],
    protections: dict[str, list[Iterable[Cell]]],
    original_ids: Iterable[str],
) -> BatchResult:
    """Lexicographically minimal full delivery in application-number order.

    The whole batch must ship.  Among assignments that ship every label we
    choose the lexicographically smallest vector of composition indices read in
    application-number order (composition 0 is the curator's first preference).
    If no full assignment exists we report blocked and explain the first label
    that cannot be satisfied.
    """

    ordered = sorted(labels, key=lambda l: (l.application_no, l.id))
    known = list(original_ids)

    def state_after(solved: Sequence[SolvedLabel]) -> dict[str, set[Cell]]:
        state = {orig: set(cells) for orig, cells in initial_public.items()}
        for item in solved:
            for orig in item.check.originals:
                state.setdefault(orig, set()).update(
                    item.composition.crop_cells_by_original().get(orig, set())
                )
        return state

    def search(index: int, public: dict[str, set[Cell]]) -> list[SolvedLabel] | None:
        if index == len(ordered):
            return []
        label = ordered[index]
        for comp in label.compositions:
            check = check_composition(
                comp,
                panel=label.panel,
                reserved=label.reserved,
                signature_lengths=[label.signature_length] * len(comp.blocks),
                license_region=label.license_region,
                public_cells=public,
                protections=protections,
                original_ids=known,
            )
            if not check.feasible:
                continue
            new_public = {orig: set(cells) for orig, cells in public.items()}
            for orig in check.originals:
                new_public.setdefault(orig, set()).update(
                    comp.crop_cells_by_original().get(orig, set())
                )
            rest = search(index + 1, new_public)
            if rest is not None:
                return [SolvedLabel(label.id, label.application_no, comp, check)] + rest
        return None

    solution = search(0, {orig: set(c) for orig, c in initial_public.items()})
    if solution is not None:
        final = state_after(solution)
        return BatchResult(
            status="delivered",
            order=[s.label_id for s in solution],
            solved=solution,
            public_after={orig: sorted(cells) for orig, cells in final.items()},
        )

    # blocked: walk in order, greedily, and explain the first impossible label
    public = {orig: set(c) for orig, c in initial_public.items()}
    diagnostics: list[dict[str, Any]] = []
    for label in ordered:
        feasible_here: list[tuple[Composition, CompositionCheck]] = []
        per_comp = []
        for comp in label.compositions:
            check = check_composition(
                comp,
                panel=label.panel,
                reserved=label.reserved,
                signature_lengths=[label.signature_length] * len(comp.blocks),
                license_region=label.license_region,
                public_cells=public,
                protections=protections,
                original_ids=known,
            )
            per_comp.append(check)
            if check.feasible:
                feasible_here.append((comp, check))
        if not feasible_here:
            diagnostics.append(
                {
                    "blocking_label": label.id,
                    "application_no": label.application_no,
                    "compositions": [c.as_dict() for c in per_comp],
                    "explanation": _explain_block(label, per_comp),
                }
            )
            break
        # commit the preferred feasible composition to keep walking
        pick_comp, _pick_check = feasible_here[0]
        for orig, cells in pick_comp.crop_cells_by_original().items():
            public.setdefault(orig, set()).update(cells)
    return BatchResult(
        status="blocked",
        order=[l.id for l in ordered],
        diagnostics=diagnostics,
    )


def _explain_block(
    label: Label, checks: Sequence[CompositionCheck]
) -> str:
    notes = []
    unlicensed = any(
        any(r.startswith("unlicensed") for r in c.reasons) for c in checks
    )
    signature = any(
        any("signature" in r or "reserved" in r for r in c.reasons) for c in checks
    )
    protection = any(
        any("protection group" in r for r in c.reasons) for c in checks
    )
    if unlicensed:
        notes.append("许可不足（裁切超出授权区域）")
    if signature:
        notes.append("署名放不下（版面/馆徽占用）")
    if protection:
        notes.append("累计公开会补全禁止公开的像格组")
    if not notes:
        notes.append("无可行构图")
    return f"{label.id}: " + "; ".join(notes)


# --------------------------------------------------------------- ascii art


def render_original(original: Original, *, highlight: Iterable[Cell] = ()) -> str:
    marks = set(highlight)
    lines = ["   " + " ".join(f"{c}" for c in range(original.cols))]
    for r in range(original.rows):
        row = []
        for c in range(original.cols):
            row.append("X" if (r, c) in marks else f"{original.index((r, c)) % 10}")
        lines.append(f"{r}: " + " ".join(row))
    return "\n".join(lines)


def render_panel(
    comp: Composition,
    *,
    panel: Panel,
    signature_cells: Iterable[Cell] = (),
    reserved: Iterable[Cell] = (),
) -> str:
    rows, cols = panel
    picture: dict[Cell, int] = {}
    for index, block in enumerate(comp.blocks):
        for cell in block.picture_cells():
            picture[cell] = index
    sig = set(signature_cells)
    res = set(reserved)
    lines = ["    " + " ".join(f"{c:>2}" for c in range(cols))]
    for r in range(rows):
        cells = []
        for c in range(cols):
            cell = (r, c)
            if cell in res:
                cells.append(" #")
            elif cell in picture:
                cells.append(f" {picture[cell]}")
            elif cell in sig:
                cells.append(" S")
            else:
                cells.append(" .")
        lines.append(f"{r}: " + " ".join(cells))
    return "\n".join(lines)


def render_check(
    comp: Composition,
    check: CompositionCheck,
    *,
    panel: Panel,
    reserved: Iterable[Cell] = (),
    originals: dict[str, Original] | None = None,
) -> str:
    sig_cells = (
        [(r, c) for p in check.signature.placements for r, c in p.cells]
        if check.signature
        else []
    )
    parts = [render_panel(comp, panel=panel, signature_cells=sig_cells, reserved=reserved)]
    if originals:
        for orig in sorted(check.originals):
            if orig in originals:
                parts.append(
                    f"original {orig}:\n"
                    + render_original(originals[orig], highlight=check.printed)
                )
    return "\n".join(parts)

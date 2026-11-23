"""Adjustable needle mold, punch simulation and calibration inference.

One table needle position is wired to exactly one adjustment axis, and every
needle on the same axis is driven to the *same* depth.  That coupling is the
whole point of the calibration: a setting must assign one depth per axis and is
shared by every proof sheet.

A depth of ``0`` means the needle is stopped.  A needle that fires at depth
``d`` (``d >= 1``) pushes through the **top ``d`` layers** of whatever stack is
under it; if the stack has fewer than ``d`` layers it simply goes through all
of them and stops at the table ("过底不再穿").  Nothing is punched where no
paper covers a needle ("未被纸覆盖的位置不留痕").

Calibration proof sheets only report, per original cell, whether it ended up
``punched`` (有孔) or ``intact`` (完整).  A hole is cut through a layer's
current upward face; the cell records the hole no matter which side ends up
outside after unfolding.  We therefore track whether a cell was pierced at all,
and reconstruct the unfolded pattern by asking, for each original cell, whether
any layer of it was pierced.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .errors import CalibrationContradiction, InvalidMold
from .folding import FoldPlan

PUNCHED = "punched"
INTACT = "intact"


@dataclass(frozen=True)
class NeedleMold:
    """A fixed needle table plus its axis wiring and per-axis candidate depths.

    ``axis_of_needle`` maps each needle position to an axis name.
    ``candidate_depths`` maps each axis name to the depths it may be set to;
    ``0`` (stopped) is always permitted and is added if missing.
    """

    name: str
    needle_axis: tuple[tuple[int, str], ...]
    candidate_depths: tuple[tuple[str, tuple[int, ...]], ...]

    def __post_init__(self) -> None:
        axes: dict[str, tuple[int, ...]] = {}
        for axis, depths in self.candidate_depths:
            cleaned = tuple(sorted({int(d) for d in depths if int(d) >= 0}))
            if not cleaned:
                cleaned = (0,)
            if 0 not in cleaned:
                cleaned = (0,) + cleaned
            axes[axis] = cleaned
        object.__setattr__(self, "candidate_depths", tuple(axes.items()))

        needles: dict[int, str] = {}
        for needle, axis in self.needle_axis:
            needle = int(needle)
            if needle < 0:
                raise InvalidMold("needle positions must be >= 0")
            if axis not in axes:
                raise InvalidMold(
                    f"needle {needle} wired to unknown axis {axis!r}"
                )
            if needle in needles:
                raise InvalidMold(f"needle {needle} appears more than once")
            needles[needle] = axis
        if not needles:
            raise InvalidMold("mold must wire at least one needle")
        object.__setattr__(
            self, "needle_axis", tuple(sorted(needles.items()))
        )

    # -- accessors -----------------------------------------------------
    @property
    def axes(self) -> tuple[str, ...]:
        return tuple(axis for axis, _ in self.candidate_depths)

    @property
    def needle_positions(self) -> tuple[int, ...]:
        return tuple(needle for needle, _ in self.needle_axis)

    def axis_for(self, needle: int) -> Optional[str]:
        for position, axis in self.needle_axis:
            if position == needle:
                return axis
        return None

    def depths_for(self, axis: str) -> tuple[int, ...]:
        for name, depths in self.candidate_depths:
            if name == axis:
                return depths
        raise InvalidMold(f"unknown axis {axis!r}")

    def needle_span(self) -> tuple[int, int]:
        positions = self.needle_positions
        return (min(positions), max(positions))

    def product_size(self) -> int:
        size = 1
        for _, depths in self.candidate_depths:
            size *= len(depths)
        return size

    def settings(self) -> Iterable[dict[str, int]]:
        """Iterate every candidate setting {axis: depth} in a stable order."""

        axes = self.axes
        ranges = [self.depths_for(axis) for axis in axes]
        for combo in itertools.product(*ranges):
            yield dict(zip(axes, combo))

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "needle_axis": [[n, a] for n, a in self.needle_axis],
            "candidate_depths": {a: list(d) for a, d in self.candidate_depths},
            "product_size": self.product_size(),
        }


@dataclass(frozen=True)
class PunchSheet:
    """One calibration sheet: a fold plan, stop step, layout and observations.

    ``observations`` maps original cell -> PUNCHED/INTACT for the cells the
    inspector actually ticked.  ``pierce_count`` optionally records "exactly N
    original cells were pierced" as an aggregate check.
    """

    sheet_id: str
    plan: FoldPlan
    steps: int
    start: int
    observations: tuple[tuple[str, str], ...]
    pierce_count: Optional[int] = None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise InvalidMold("sheet start needle position must be >= 0")
        if self.steps < 0 or self.steps > len(self.plan.directions):
            raise InvalidMold(
                f"sheet {self.sheet_id!r} stops at step {self.steps} but the plan "
                f"has {len(self.plan.directions)} folds"
            )
        clean: list[tuple[str, str]] = []
        seen: set[str] = set()
        for cell, verdict in self.observations:
            if verdict not in (PUNCHED, INTACT):
                raise InvalidMold(
                    f"sheet {self.sheet_id!r} observation {cell!r} must be "
                    f"{PUNCHED!r} or {INTACT!r}, got {verdict!r}"
                )
            if cell in seen:
                raise InvalidMold(
                    f"sheet {self.sheet_id!r} observes cell {cell!r} twice"
                )
            if cell not in self.plan.cells:
                raise InvalidMold(
                    f"sheet {self.sheet_id!r} observes unknown cell {cell!r}"
                )
            seen.add(cell)
            clean.append((cell, verdict))
        object.__setattr__(self, "observations", tuple(clean))
        if self.pierce_count is not None and self.pierce_count < 0:
            raise InvalidMold("pierce_count must be >= 0")

    @property
    def observed_cells(self) -> tuple[str, ...]:
        return tuple(cell for cell, _ in self.observations)


def _punched_cells(plan: FoldPlan, steps: int, start: int, setting: dict[str, int],
                   mold: NeedleMold) -> set[str]:
    """Cells pierced when ``mold`` fires under ``setting`` on a folded strip."""

    state = plan.state_after(steps)
    punched: set[str] = set()
    for needle in mold.needle_positions:
        depth = setting[mold.axis_for(needle)]
        if depth <= 0:
            continue
        offset = needle - start
        if offset < 0 or offset >= len(state):
            continue  # no paper here -> no mark
        stack = state[offset]
        for layer in stack[:depth]:
            punched.add(layer.cell)
    return punched


def _explains(sheet: PunchSheet, mold: NeedleMold, punched: set[str]) -> bool:
    for cell, verdict in sheet.observations:
        actual = PUNCHED if cell in punched else INTACT
        if actual != verdict:
            return False
    if sheet.pierce_count is not None and len(punched) != sheet.pierce_count:
        return False
    return True


def unfold_pattern(sheet: PunchSheet, mold: NeedleMold,
                   setting: dict[str, int]) -> dict[str, str]:
    """Per-original-cell punched/intact pattern for a given setting."""

    punched = _punched_cells(sheet.plan, sheet.steps, sheet.start, setting, mold)
    return {
        cell: (PUNCHED if cell in punched else INTACT) for cell in sheet.plan.cells
    }


def compatible_settings(sheets: Sequence[PunchSheet], mold: NeedleMold
                        ) -> list[dict[str, int]]:
    """Every candidate setting that explains *all* sheets simultaneously."""

    compatible: list[dict[str, int]] = []
    for setting in mold.settings():
        ok = True
        for sheet in sheets:
            punched = _punched_cells(
                sheet.plan, sheet.steps, sheet.start, setting, mold
            )
            if not _explains(sheet, mold, punched):
                ok = False
                break
        if ok:
            compatible.append(setting)
    return compatible


def sheets_contradictory(sheets: Sequence[PunchSheet], mold: NeedleMold) -> bool:
    """True when no single setting can explain every sheet at once."""

    return not compatible_settings(sheets, mold)


@dataclass(frozen=True)
class OrderRequirement:
    """A work order: certain cells must be punched and/or left intact."""

    order_id: str
    must_punch: tuple[str, ...] = ()
    must_intact: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        must_punch = tuple(str(c) for c in self.must_punch)
        must_intact = tuple(str(c) for c in self.must_intact)
        overlap = set(must_punch) & set(must_intact)
        if overlap:
            raise InvalidMold(
                f"order {self.order_id!r} both punches and keeps "
                f"{sorted(overlap)}"
            )
        object.__setattr__(self, "must_punch", must_punch)
        object.__setattr__(self, "must_intact", must_intact)

    def cells(self) -> tuple[str, ...]:
        seen: list[str] = []
        for cell in self.must_punch + self.must_intact:
            if cell not in seen:
                seen.append(cell)
        return tuple(seen)

    def satisfied_by(self, pattern: dict[str, str]) -> bool:
        for cell in self.must_punch:
            if pattern.get(cell) != PUNCHED:
                return False
        for cell in self.must_intact:
            if pattern.get(cell) != INTACT:
                return False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "must_punch": list(self.must_punch),
            "must_intact": list(self.must_intact),
        }


@dataclass(frozen=True)
class OrderJudgment:
    """Verdict for an order against every setting compatible with calibration.

    ``verdict`` is one of:
    * ``"go"``        -- every compatible setting satisfies the order.
    * ``"hold"``      -- some settings satisfy it, some do not (needs two
                         witnesses).
    * ``"return"``    -- no compatible setting satisfies it.
    * ``"contradiction"`` -- the calibration itself admits no setting.
    """

    order: OrderRequirement
    verdict: str
    passing_settings: tuple[dict[str, int], ...]
    failing_settings: tuple[dict[str, int], ...]
    patterns: tuple[dict[str, object], ...] = ()

    @property
    def approved(self) -> bool:
        return self.verdict == "go"

    def to_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order.order_id,
            "verdict": self.verdict,
            "passing_settings": [dict(s) for s in self.passing_settings],
            "failing_settings": [dict(s) for s in self.failing_settings],
            "patterns": list(self.patterns),
        }


def _pattern_for(order: OrderRequirement, sheet: PunchSheet, mold: NeedleMold,
                 setting: dict[str, int]) -> dict[str, object]:
    pattern = unfold_pattern(sheet, mold, setting)
    return {
        "setting": dict(setting),
        "pattern": pattern,
        "cells": [
            {"cell": cell, "state": pattern[cell]}
            for cell in order.cells()
        ],
    }


def judge_order(order: OrderRequirement, layout: PunchSheet,
                sheets: Sequence[PunchSheet], mold: NeedleMold) -> OrderJudgment:
    """Decide an order laid out on ``layout`` over *all* compatible settings.

    ``sheets`` is the retained calibration evidence; ``layout`` is the geometry
    the customer order is actually folded onto (it may have its own stop step
    or start position).  This is a thin wrapper over
    :func:`judge_order_on_sheet` that first derives the compatible settings.
    """

    compatible = compatible_settings(sheets, mold)
    return judge_order_on_sheet(order, layout, compatible, mold)


def judge_order_on_sheet(order: OrderRequirement, sheet: PunchSheet,
                         compatible: Sequence[dict[str, int]], mold: NeedleMold
                         ) -> OrderJudgment:
    """Judge an order against a *given* layout sheet and compatible settings.

    Used when a new order arrives with its own fold diagram (possibly a new
    stop step or start position), while the calibration settings stay fixed.
    """

    if not compatible:
        return OrderJudgment(
            order=order,
            verdict="contradiction",
            passing_settings=(),
            failing_settings=(),
        )
    passing: list[dict[str, int]] = []
    failing: list[dict[str, int]] = []
    for setting in compatible:
        if order.satisfied_by(unfold_pattern(sheet, mold, setting)):
            passing.append(setting)
        else:
            failing.append(setting)
    if passing and failing:
        verdict = "hold"
    elif passing:
        verdict = "go"
    else:
        verdict = "return"
    patterns: tuple[dict[str, object], ...] = ()
    if verdict in ("go", "hold"):
        witnesses = [passing[0]]
        if verdict == "hold":
            witnesses.append(failing[0])
        patterns = tuple(
            _pattern_for(order, sheet, mold, setting) for setting in witnesses
        )
    return OrderJudgment(
        order=order,
        verdict=verdict,
        passing_settings=tuple(passing),
        failing_settings=tuple(failing),
        patterns=patterns,
    )


@dataclass(frozen=True)
class WorkOrder:
    """A customer order placed on its own fold diagram (its chosen "译本").

    ``plan``/``steps``/``start`` describe how the strip is laid on the mold for
    *this* order, which may differ from the calibration sheets.  The set of
    admissible settings, however, always comes from the retained calibration.
    """

    order_id: str
    plan: FoldPlan
    steps: int
    start: int
    must_punch: tuple[str, ...] = ()
    must_intact: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        req = OrderRequirement(self.order_id, self.must_punch, self.must_intact)
        object.__setattr__(self, "must_punch", req.must_punch)
        object.__setattr__(self, "must_intact", req.must_intact)
        if self.start < 0:
            raise InvalidMold("order start needle position must be >= 0")
        if self.steps < 0 or self.steps > len(self.plan.directions):
            raise InvalidMold(
                f"order {self.order_id!r} stops at step {self.steps} but the "
                f"plan has {len(self.plan.directions)} folds"
            )

    def requirement(self) -> OrderRequirement:
        return OrderRequirement(self.order_id, self.must_punch, self.must_intact)

    def layout_sheet(self) -> PunchSheet:
        """A geometry-only sheet (no cell observations) for this order."""

        return PunchSheet(
            sheet_id=f"order:{self.order_id}",
            plan=self.plan,
            steps=self.steps,
            start=self.start,
            observations=(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "cells": list(self.plan.cells),
            "directions": list(self.plan.directions),
            "steps": self.steps,
            "start": self.start,
            "must_punch": list(self.must_punch),
            "must_intact": list(self.must_intact),
        }


def judge_work_order(order: WorkOrder, sheets: Sequence[PunchSheet],
                     mold: NeedleMold, *, compatible: Optional[Sequence[dict[str, int]]] = None
                     ) -> OrderJudgment:
    """Judge an order on its own layout over every calibration-compatible setting."""

    if compatible is None:
        compatible = compatible_settings(sheets, mold)
    return judge_order_on_sheet(
        order.requirement(), order.layout_sheet(), compatible, mold
    )


def naive_shared_axis_verdict(order: WorkOrder, compatible: Sequence[dict[str, int]],
                              mold: NeedleMold) -> str:
    """What a naive per-needle analysis *would* say, ignoring the shared axis.

    A careless reader treats every needle depth as independently choosable and
    declares the order feasible if *some* per-needle depths (each taken from a
    compatible setting's value for that needle's axis, but possibly from
    different settings) together satisfy the order.  Two needles on one axis
    cannot actually take different depths, so this over-approves.
    """

    sheet = order.layout_sheet()
    req = order.requirement()
    if not compatible:
        return "contradiction"

    # Per (needle) candidate depth options, drawn from *any* compatible setting.
    needle_options: dict[int, set[int]] = {n: set() for n in mold.needle_positions}
    for setting in compatible:
        for needle in mold.needle_positions:
            needle_options[needle].add(setting[mold.axis_for(needle)])

    # Try every combination of independent per-needle depths.
    ranges = [sorted(needle_options[n]) for n in mold.needle_positions]
    for combo in itertools.product(*ranges):
        setting = {n: d for n, d in zip(mold.needle_positions, combo)}
        if req.satisfied_by(_pattern_from_needle_depths(sheet, mold, setting)):
            return "go"
    return "return"


def _pattern_from_needle_depths(sheet: PunchSheet, mold: NeedleMold,
                                needle_depth: dict[int, int]) -> dict[str, str]:
    """Unfolded cell pattern when each needle may take an individual depth."""

    state = sheet.plan.state_after(sheet.steps)
    punched: set[str] = set()
    for needle, depth in needle_depth.items():
        if depth <= 0:
            continue
        offset = needle - sheet.start
        if offset < 0 or offset >= len(state):
            continue
        for layer in state[offset][:depth]:
            punched.add(layer.cell)
    return {
        cell: (PUNCHED if cell in punched else INTACT) for cell in sheet.plan.cells
    }



def require_consistent(sheets: Sequence[PunchSheet], mold: NeedleMold) -> list[dict[str, int]]:
    """Return compatible settings or raise :class:`CalibrationContradiction`."""

    compatible = compatible_settings(sheets, mold)
    if not compatible:
        raise CalibrationContradiction(
            f"no single mold setting explains all {len(sheets)} calibration "
            f"sheet(s) for mold {mold.name!r}"
        )
    return compatible

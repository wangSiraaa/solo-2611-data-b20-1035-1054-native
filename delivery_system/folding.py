"""Paper-strip folding engine for strip window-flower reading.

A strip is split into ``2**k`` equal-width cells.  Each cell has a stable name
and a facing side (``front`` or ``back``).  A cell is one physical layer, so
after folding every table position holds a stack of *distinct* cells ordered
top -> bottom.

A fold lifts one half of the *current* paper width over the centre line and
presses it onto the other half:

* ``"L"`` -- lift the left half and fold it onto the right half.
* ``"R"`` -- lift the right half and fold it onto the left half.

When a lifted stack is moved across the centre, its top/bottom order reverses
and every lifted cell's facing side flips.  After each fold the positions are
recounted from the left of the table.  This module is deliberately independent
of translations and packages: text stays opaque elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .errors import InvalidFoldPlan

FRONT = 0
BACK = 1
SIDE_NAMES = {FRONT: "front", BACK: "back"}

State = tuple[tuple["Layer", ...], ...]


def flip_side(side_up: int) -> int:
    """Return the opposite side (front <-> back)."""

    return side_up ^ 1


def _normalize_direction(direction: str) -> str:
    key = str(direction).strip().upper()
    aliases = {
        "L": "L",
        "LEFT": "L",
        "L2R": "L",
        "R": "R",
        "RIGHT": "R",
        "R2L": "R",
    }
    if key not in aliases:
        raise InvalidFoldPlan(
            f"fold direction {direction!r} is not 'L' (left onto right) or "
            f"'R' (right onto left)"
        )
    return aliases[key]


def _is_power_of_two(n: int) -> bool:
    return n >= 1 and (n & (n - 1)) == 0


@dataclass(frozen=True)
class Layer:
    """One cell sitting in a folded stack with a named upward-facing side."""

    cell: str
    side_up: int

    @property
    def side(self) -> str:
        return SIDE_NAMES[self.side_up]


def initial_state(cells: Iterable[str]) -> State:
    """Lay the strip flat on the table, one front-up layer per position."""

    ordered = tuple(str(c) for c in cells)
    _validate_cells(ordered)
    return tuple((Layer(cell, FRONT),) for cell in ordered)


def _validate_cells(cells: tuple[str, ...]) -> None:
    if not _is_power_of_two(len(cells)):
        raise InvalidFoldPlan(
            f"a strip must have a power-of-two number of cells, got {len(cells)}"
        )
    if len(set(cells)) != len(cells):
        raise InvalidFoldPlan("strip cell names must be unique")


def _flip_stack(stack: tuple[Layer, ...]) -> tuple[Layer, ...]:
    """Flip a whole lifted stack: order reverses, each cell's side flips."""

    return tuple(Layer(layer.cell, flip_side(layer.side_up)) for layer in reversed(stack))


def fold_state(state: State, direction: str) -> State:
    """Apply one fold to a folded state and return the new (narrower) state."""

    direction = _normalize_direction(direction)
    width = len(state)
    if width < 2 or width % 2 != 0:
        raise InvalidFoldPlan(
            f"cannot fold a width-{width} strip (needs an even width >= 2)"
        )
    half = width // 2
    new: list[Optional[tuple[Layer, ...]]] = [None] * half

    if direction == "L":
        # Left half is lifted, flipped and dropped on top of the right half.
        for p in range(half):
            target = width - 1 - p
            new[half - 1 - p] = _flip_stack(state[p]) + state[target]
    else:  # "R"
        # Right half is lifted, flipped and dropped on top of the left half.
        for j in range(half, width):
            target = width - 1 - j
            new[target] = _flip_stack(state[j]) + state[target]

    if any(entry is None for entry in new):  # pragma: no cover - defensive
        raise AssertionError("fold produced an unfilled position")
    return tuple(entry for entry in new if entry is not None)


@dataclass(frozen=True)
class FoldPlan:
    """An ordered fold diagram for a named strip.

    ``directions`` is the full crease sequence; a reader may stop early by
    folding only the first ``steps`` creases ("折到哪步").
    """

    cells: tuple[str, ...]
    directions: tuple[str, ...]

    def __post_init__(self) -> None:
        cells = tuple(str(c) for c in self.cells)
        directions = tuple(_normalize_direction(d) for d in self.directions)
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "directions", directions)
        _validate_cells(cells)
        max_steps = len(cells).bit_length() - 1
        if len(directions) > max_steps:
            raise InvalidFoldPlan(
                f"a {len(cells)}-cell strip supports at most {max_steps} folds, "
                f"got {len(directions)}"
            )

    @property
    def max_steps(self) -> int:
        return len(self.cells).bit_length() - 1

    def state_after(self, steps: int) -> State:
        """Fold the first ``steps`` creases and return the resulting state."""

        if steps < 0 or steps > len(self.directions):
            raise InvalidFoldPlan(
                f"cannot fold {steps} steps; plan has {len(self.directions)} creases"
            )
        state = initial_state(self.cells)
        for direction in self.directions[:steps]:
            state = fold_state(state, direction)
        return state

    def states(self) -> list[State]:
        """Every state from the flat strip (step 0) to the fully folded one."""

        out: list[State] = [initial_state(self.cells)]
        for direction in self.directions:
            out.append(fold_state(out[-1], direction))
        return out


@dataclass(frozen=True)
class StripVersion:
    """A published strip diagram: direction plus the positions you may crease.

    ``foldable_steps`` names the 0-based crease indices the diagram permits; an
    empty tuple means every step may be folded.
    """

    document_id: str
    revision: int
    cells: tuple[str, ...]
    direction: str = "L"
    foldable_steps: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        cells = tuple(str(c) for c in self.cells)
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "direction", _normalize_direction(self.direction))
        _validate_cells(cells)
        for step in self.foldable_steps:
            if step < 0 or step >= len(cells).bit_length() - 1:
                raise InvalidFoldPlan(
                    f"foldable step {step} outside 0..{len(cells).bit_length() - 2}"
                )

    def allows_step(self, step: int) -> bool:
        return not self.foldable_steps or step in self.foldable_steps

    def check_plan(self, plan: FoldPlan, steps: int) -> None:
        """Raise unless ``plan`` (folded ``steps`` deep) fits this version."""

        if plan.cells != self.cells:
            raise InvalidFoldPlan(
                "fold plan cells do not match the published strip version"
            )
        if steps < 0 or steps > len(plan.directions):
            raise InvalidFoldPlan(
                f"cannot fold {steps} steps; the plan has {len(plan.directions)}"
            )
        if steps and plan.directions[0] != self.direction:
            raise InvalidFoldPlan(
                f"fold direction {plan.directions[0]!r} does not match the "
                f"published direction {self.direction!r}"
            )
        for step in range(steps):
            if not self.allows_step(step):
                raise InvalidFoldPlan(
                    f"step {step} is not a foldable position on this version"
                )


@dataclass(frozen=True)
class FoldVoucher:
    """A queriable fold-layer voucher for one legal intermediate step.

    Positions are recounted from the left of the table.  ``start`` is the
    table needle position that the strip's leftmost position sits on, so a
    reader can ask by needle position or by original cell.
    """

    plan: FoldPlan
    steps: int
    start: int = 0

    def __post_init__(self) -> None:
        if self.steps < 0 or self.steps > len(self.plan.directions):
            raise InvalidFoldPlan(
                f"cannot issue a voucher for step {self.steps}"
            )
        if self.start < 0:
            raise InvalidFoldPlan("start needle position must be >= 0")

    @property
    def state(self) -> State:
        return self.plan.state_after(self.steps)

    def _position(self, offset: int) -> Optional[tuple[Layer, ...]]:
        state = self.state
        if 0 <= offset < len(state):
            return state[offset]
        return None

    def needle_span(self) -> tuple[int, int]:
        state = self.state
        return (self.start, self.start + len(state) - 1)

    def layers_at_needle(self, needle: int) -> list[dict[str, object]]:
        """Top -> bottom layers sitting under one table needle position."""

        stack = self._position(needle - self.start)
        if stack is None:
            return []
        return [
            {
                "needle": needle,
                "position": needle - self.start,
                "depth": rank,
                "cell": layer.cell,
                "side": layer.side,
                "side_up": layer.side_up,
            }
            for rank, layer in enumerate(stack)
        ]

    def by_needle(self) -> dict[int, list[dict[str, object]]]:
        """Every occupied needle position with its layers top -> bottom."""

        lo, hi = self.needle_span()
        return {needle: self.layers_at_needle(needle) for needle in range(lo, hi + 1)}

    def locate(self, cell: str) -> Optional[dict[str, object]]:
        """Where an original cell currently is: needle position and layer depth."""

        state = self.state
        for offset, stack in enumerate(state):
            for rank, layer in enumerate(stack):
                if layer.cell == cell:
                    return {
                        "cell": cell,
                        "needle": self.start + offset,
                        "position": offset,
                        "depth": rank,
                        "side": layer.side,
                        "side_up": layer.side_up,
                    }
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "cells": list(self.plan.cells),
            "directions": list(self.plan.directions),
            "steps": self.steps,
            "start": self.start,
            "needle_span": list(self.needle_span()),
            "by_needle": [
                {"needle": needle, "layers": layers}
                for needle, layers in self.by_needle().items()
            ],
            "by_cell": [
                located
                for located in (
                    self.locate(cell) for cell in self.plan.cells
                )
                if located is not None
            ],
        }

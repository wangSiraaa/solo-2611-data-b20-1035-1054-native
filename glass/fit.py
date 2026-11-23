"""Fit engine: turn survey measurements into admissible pane sizes.

A quantity record (``量单``) is measured on the bench.  It carries

* ``slot``            which named frame groove it belongs to
* ``install_position`` where the pane sits within the slot
* ``item``            the measurement project
* ``revision``        the signing round (``签次``)
* ``rotation``        quarter turns the frame had been turned clockwise

Items
-----
``opening``   width/height of the clear opening, on the bench
``min_lap``   bead-covered minimal lap for one side
``max_lap``   deepest lap the rebate can swallow for one side
``lap``       convenience: both bounds for one side in one record
``corner``    baffle reading for one corner (45-degree stop)
``grain``     grain axis required on the installation front

Everything that is directional (side / corner / grain axis / opening width
and height) is rotated *back* to the installation front by ``rotation``
counter-clockwise quarter turns.  Lengths are unchanged.

Sizing
------
For one position, let ``L,R,T,B`` be the chosen integer laps on left, right,
top and bottom, each inside its own ``[min_lap, max_lap]``.  Then

    width  = opening_width  + L + R
    height = opening_height + T + B

and every corner with a baffle reading ``c`` must satisfy the sum of its two
adjacent laps ``<= c``.  Because the corners couple the horizontal and the
vertical laps, the individually largest width and height need not be
achievable together; :func:`maximal_sizes` returns only sizes that are jointly
feasible and Pareto-maximal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import geom

INF = 10**9


class FitError(Exception):
    """A measurement is missing, ambiguous or must be re-taken."""


@dataclass(frozen=True)
class Reading:
    slot: str
    install_position: str
    item: str
    revision: int
    target: str = ""          # side or corner, empty for opening/grain
    payload: tuple = ()       # normalised, rotation-free reading

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.slot, self.install_position, self.item, self.target)


# --------------------------------------------------------------------- input


def _as_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FitError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def normalize_quantity(raw: Mapping[str, Any]) -> list[Reading]:
    """Normalise one raw record into one or more rotation-free readings."""

    slot = raw.get("slot")
    if not isinstance(slot, str) or not slot:
        raise FitError("quantity needs a non-empty 'slot'")
    position = raw.get("install_position", "single")
    if not isinstance(position, str) or not position:
        raise FitError("install_position must be a non-empty string")
    item = raw.get("item")
    revision = _as_int("revision", raw.get("revision"))
    if revision < 1:
        raise FitError("revision must be >= 1 (1 is the first signing round)")
    rotation = raw.get("rotation", 0)
    if not isinstance(rotation, int) or isinstance(rotation, bool):
        raise FitError("rotation must be an integer number of quarter turns")
    rotation %= 4

    def make(one_item: str, target: str, payload: tuple) -> Reading:
        return Reading(slot, position, one_item, revision, target, payload)

    if item == "opening":
        width = _as_int("opening width", raw.get("width"))
        height = _as_int("opening height", raw.get("height"))
        iw, ih = geom.bench_dims_to_install(width, height, rotation)
        return [make("opening", "", (iw, ih))]

    if item in ("min_lap", "max_lap"):
        side = geom.bench_to_install_side(raw.get("side"), rotation)
        value = _as_int("lap", raw.get("value"))
        return [make(item, side, (value,))]

    if item == "lap":
        side = geom.bench_to_install_side(raw.get("side"), rotation)
        lo = _as_int("min lap", raw.get("min", 0))
        hi = _as_int("max lap", raw.get("max", lo))
        if hi < lo:
            raise FitError(f"max lap {hi} is below min lap {lo} on side {side}")
        return [make("min_lap", side, (lo,)), make("max_lap", side, (hi,))]

    if item == "corner":
        corner = geom.bench_to_install_corner(raw.get("corner"), rotation)
        value = _as_int("corner baffle", raw.get("value"))
        return [make("corner", corner, (value,))]

    if item == "grain":
        axis = raw.get("axis")
        if axis not in geom.AXES:
            raise FitError(f"grain axis must be one of {geom.AXES}, got {axis!r}")
        return [make("grain", "", (geom.bench_to_install_axis(axis, rotation),))]

    raise FitError(f"unknown measurement item {item!r}")


def select_readings(raws: Iterable[Mapping[str, Any]]) -> dict[tuple, Reading]:
    """Highest active signing round per (slot, position, item, target).

    Two *different* readings at the same active round mean the slip must be
    re-taken; identical duplicates collapse.  Withdrawal is handled upstream
    by only feeding active records here.
    """

    chosen: dict[tuple, Reading] = {}
    for raw in raws:
        for reading in normalize_quantity(raw):
            key = reading.key
            current = chosen.get(key)
            if current is None or reading.revision > current.revision:
                chosen[key] = reading
            elif reading.revision == current.revision:
                if reading.payload != current.payload:
                    raise FitError(
                        "conflicting readings at the same signing round for "
                        f"{reading.item} on slot {reading.slot}/"
                        f"{reading.install_position}{(' ' + reading.target) if reading.target else ''}; "
                        "re-take the measurement"
                    )
    return chosen


# ---------------------------------------------------------------- requirement


@dataclass
class SideLap:
    minimum: int = 0
    maximum: int = 0

    @property
    def fixed(self) -> bool:
        return self.minimum == self.maximum


@dataclass
class PositionRequirement:
    slot: str
    position: str
    color: str
    opening: tuple[int, int] | None = None
    min_lap: dict[str, int] = field(default_factory=dict)
    max_lap: dict[str, int] = field(default_factory=dict)
    corner_limit: dict[str, int] = field(default_factory=dict)
    grain: str | None = None
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ laps
    def lap(self, side: str) -> SideLap:
        low = self.min_lap.get(side, 0)
        high = self.max_lap.get(side, low)
        if high < low:
            high = low
        return SideLap(low, high)

    def requirement_errors(self) -> list[str]:
        errors = []
        if self.opening is None:
            errors.append("missing clear-opening measurement")
        for side in geom.SIDES:
            if side not in self.min_lap:
                errors.append(f"missing minimum lap on {side}")
            if side not in self.max_lap:
                errors.append(f"missing maximum lap on {side}")
        if self.grain is not None and self.grain not in geom.AXES:
            errors.append(f"invalid grain axis {self.grain!r}")
        return errors

    # --------------------------------------------------------------- sizing
    def _ranges(self) -> dict[str, tuple[int, int]]:
        return {side: (self.lap(side).minimum, self.lap(side).maximum)
                for side in geom.SIDES}

    def feasible_laps(
        self, width: int, height: int
    ) -> dict[str, int] | None:
        """Return one concrete lap assignment realising ``width x height``.

        ``None`` means the size cannot be fitted.  The returned assignment is
        deterministic (smallest left, then smallest top) so explanations are
        stable.
        """

        if self.opening is None:
            return None
        ow, oh = self.opening
        wd = width - ow
        hd = height - oh
        if wd < 0 or hd < 0:
            return None
        r = self._ranges()
        (lmin, lmax), (rmin, rmax) = r["left"], r["right"]
        (tmin, tmax), (bmin, bmax) = r["top"], r["bottom"]

        l_lo = max(lmin, wd - rmax)
        l_hi = min(lmax, wd - rmin)
        if l_lo > l_hi:
            return None
        t_lo = max(tmin, hd - bmax)
        t_hi = min(tmax, hd - bmin)
        if t_lo > t_hi:
            return None

        for left in range(l_lo, l_hi + 1):
            right = wd - left
            for top in range(t_lo, t_hi + 1):
                bottom = hd - top
                if not self._corners_ok(left, right, top, bottom):
                    continue
                return {
                    "left": left,
                    "right": right,
                    "top": top,
                    "bottom": bottom,
                }
        return None

    def _corners_ok(self, left: int, right: int, top: int, bottom: int) -> bool:
        pairs = {
            "TR": (top, right),
            "BR": (right, bottom),
            "BL": (bottom, left),
            "TL": (left, top),
        }
        for corner, (a, b) in pairs.items():
            limit = self.corner_limit.get(corner)
            if limit is not None and a + b > limit:
                return False
        return True

    def explain(self, width: int, height: int) -> dict:
        """Explain how a size laps in, or exactly why it does not fit."""

        if self.opening is None:
            return {"accepted": False, "reason": "missing clear-opening"}
        ow, oh = self.opening
        laps = self.feasible_laps(width, height)
        if laps is not None:
            return {
                "accepted": True,
                "width": width,
                "height": height,
                "opening": [ow, oh],
                "laps": laps,
                "corners": {
                    c: {"limit": self.corner_limit.get(c)}
                    for c in geom.CORNERS
                    if c in self.corner_limit
                },
            }
        wd = width - ow
        hd = height - oh
        if wd < 0 or hd < 0:
            reason = "pane smaller than the clear opening"
        else:
            r = self._ranges()
            reason = self._diagnose(wd, hd, r)
        return {
            "accepted": False,
            "width": width,
            "height": height,
            "opening": [ow, oh],
            "reason": reason,
        }

    def _diagnose(self, wd: int, hd: int, r: Mapping[str, tuple[int, int]]) -> str:
        (lmin, lmax), (rmin, rmax) = r["left"], r["right"]
        (tmin, tmax), (bmin, bmax) = r["top"], r["bottom"]
        if not (lmin + rmin <= wd <= lmax + rmax):
            return (
                f"horizontal lap sum {wd} outside [{lmin + rmin}, {lmax + rmax}] "
                "allowed by left/right beads and rebates"
            )
        if not (tmin + bmin <= hd <= tmax + bmax):
            return (
                f"vertical lap sum {hd} outside [{tmin + bmin}, {tmax + bmax}] "
                "allowed by top/bottom beads and rebates"
            )
        # Pinpoint a failing corner for a natural 2x2-style refusal.
        for left in range(max(lmin, wd - rmax), min(lmax, wd - rmin) + 1):
            right = wd - left
            for top in range(max(tmin, hd - bmax), min(tmax, hd - bmin) + 1):
                bottom = hd - top
                for corner, (a, b) in {
                    "TR": (top, right),
                    "BR": (right, bottom),
                    "BL": (bottom, left),
                    "TL": (left, top),
                }.items():
                    limit = self.corner_limit.get(corner)
                    if limit is not None and a + b > limit:
                        return (
                            f"corner {corner} baffle {limit} exceeded by adjacent "
                            f"laps {a}+{b}={a + b} for every admissible lap split"
                        )
        return "no integer lap assignment satisfies all corner baffles"

    def maximal_sizes(self, limit: int = 400_000) -> list[dict]:
        """Jointly feasible, Pareto-maximal sizes for this position."""

        if self.opening is None:
            return []
        ow, oh = self.opening
        r = self._ranges()
        w_lo = ow + r["left"][0] + r["right"][0]
        w_hi = ow + r["left"][1] + r["right"][1]
        h_lo = oh + r["top"][0] + r["bottom"][0]
        h_hi = oh + r["top"][1] + r["bottom"][1]
        if (w_hi - w_lo + 1) * (h_hi - h_lo + 1) > limit:
            raise FitError(
                "lap ranges are too wide to enumerate; bound the maximal laps"
            )
        feasible: list[tuple[int, int]] = []
        for w in range(w_lo, w_hi + 1):
            for h in range(h_lo, h_hi + 1):
                if self.feasible_laps(w, h) is not None:
                    feasible.append((w, h))
        feasible_set = set(feasible)
        maximal = [
            (w, h)
            for (w, h) in feasible
            if not any(
                (w2 >= w and h2 >= h and (w2 > w or h2 > h))
                for (w2, h2) in feasible_set
            )
        ]
        maximal.sort()
        out = []
        for w, h in maximal:
            laps = self.feasible_laps(w, h)
            out.append({"width": w, "height": h, "laps": laps})
        return out

    def max_width(self) -> int | None:
        sizes = self.maximal_sizes()
        return max((s["width"] for s in sizes), default=None)

    def max_height(self) -> int | None:
        sizes = self.maximal_sizes()
        return max((s["height"] for s in sizes), default=None)


def build_requirement(
    slot: str,
    color: str,
    position: str,
    readings: Mapping[tuple, Reading],
) -> PositionRequirement:
    req = PositionRequirement(slot=slot, position=position, color=color)
    for (r_slot, r_pos, item, target), reading in readings.items():
        if r_slot != slot or r_pos != position:
            continue
        if item == "opening":
            req.opening = reading.payload  # (install width, install height)
        elif item == "min_lap":
            req.min_lap[target] = reading.payload[0]
        elif item == "max_lap":
            req.max_lap[target] = reading.payload[0]
        elif item == "corner":
            req.corner_limit[target] = reading.payload[0]
        elif item == "grain":
            req.grain = reading.payload[0]
    # Fill from convenience defaults, recording what we assumed.
    for side in geom.SIDES:
        if side not in req.min_lap and side not in req.max_lap:
            req.warnings.append(f"no lap given on {side}; assuming 0")
            req.min_lap[side] = 0
            req.max_lap[side] = 0
        elif side in req.min_lap and side not in req.max_lap:
            req.warnings.append(
                f"no maximum lap on {side}; assuming the minimum"
            )
            req.max_lap[side] = req.min_lap[side]
        elif side in req.max_lap and side not in req.min_lap:
            req.min_lap[side] = 0
    return req


def positions_for(readings: Mapping[tuple, Reading]) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for (slot, position, _item, _target) in sorted(readings):
        if (slot, position) not in seen:
            seen.append((slot, position))
    return seen

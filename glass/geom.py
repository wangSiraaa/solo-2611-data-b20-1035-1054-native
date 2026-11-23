"""Orientation helpers for the glazing domain.

Sides are ordered clockwise around the *installation front*, starting at the
top.  Corners are indexed by the side from which they start, clockwise:

    TR joins top,right ; BR joins right,bottom ; BL joins bottom,left ;
    TL joins left,top

A surveyor may measure a frame that has been turned clockwise by ``r`` quarter
turns on the bench.  To bring a bench reading back to the installation front we
undo that turn, i.e. rotate counter-clockwise by ``r``.  Lengths do not change;
only *which* side / corner / axis a reading belongs to does.
"""

from __future__ import annotations

SIDES = ("top", "right", "bottom", "left")
CORNERS = ("TR", "BR", "BL", "TL")
CORNER_SIDES = {
    "TR": ("top", "right"),
    "BR": ("right", "bottom"),
    "BL": ("bottom", "left"),
    "TL": ("left", "top"),
}
AXES = ("horizontal", "vertical")
_OPPOSITE = {"horizontal": "vertical", "vertical": "horizontal"}


def rotate_cw_side(side: str, turns: int) -> str:
    if side not in SIDES:
        raise ValueError(f"unknown side {side!r}")
    return SIDES[(SIDES.index(side) + turns) % 4]


def rotate_cw_corner(corner: str, turns: int) -> str:
    if corner not in CORNERS:
        raise ValueError(f"unknown corner {corner!r}")
    return CORNERS[(CORNERS.index(corner) + turns) % 4]


def bench_to_install_side(side: str, rotations: int) -> str:
    """Map a bench side back to the installation front."""
    return rotate_cw_side(side, -rotations)


def bench_to_install_corner(corner: str, rotations: int) -> str:
    return rotate_cw_corner(corner, -rotations)


def swap_axis(axis: str) -> str:
    if axis not in _OPPOSITE:
        raise ValueError(f"unknown grain axis {axis!r}")
    return _OPPOSITE[axis]


def bench_to_install_axis(axis: str, rotations: int) -> str:
    """Grain direction along a screen axis; a quarter turn swaps it."""
    return swap_axis(axis) if rotations % 2 else axis


def bench_dims_to_install(width: int, height: int, rotations: int) -> tuple[int, int]:
    """Opening width/height measured on a rotated frame, back to the front."""
    return (height, width) if rotations % 2 else (width, height)

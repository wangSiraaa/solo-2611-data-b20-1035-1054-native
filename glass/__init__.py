"""Glazing workshop: frame-groove resolution, pane sizing, cut planning.

Built on :mod:`cs` for the catalog, revisions, cuts and immutable products.
"""

from . import geom
from .cutting import (
    Cut,
    Defect,
    Ledger,
    PaneSpec,
    Piece,
    Plan,
    PlanError,
    Placement,
    check_placements_within_pieces,
    crop_options,
    guillotine_order,
    plan_cuts,
)
from .fit import (
    FitError,
    PositionRequirement,
    Reading,
    SideLap,
    build_requirement,
    normalize_quantity,
    positions_for,
    select_readings,
)
from .store import (
    GlassError,
    GlazingStore,
    Slot,
    SurveyResolution,
    readings_for_position,
)

__all__ = [
    "geom",
    "Cut",
    "Defect",
    "Ledger",
    "PaneSpec",
    "Piece",
    "Plan",
    "PlanError",
    "Placement",
    "check_placements_within_pieces",
    "crop_options",
    "guillotine_order",
    "plan_cuts",
    "FitError",
    "PositionRequirement",
    "Reading",
    "SideLap",
    "build_requirement",
    "normalize_quantity",
    "positions_for",
    "select_readings",
    "GlassError",
    "GlazingStore",
    "Slot",
    "SurveyResolution",
    "readings_for_position",
]

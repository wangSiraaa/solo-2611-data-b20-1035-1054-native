"""Weaving room extension: physical shuttles, loads, and weave execution tapes.

The package extends the same SQLite database used by the reagent service.
Shuttle thread is always backed by real reagent runs; the shuttle event
ledger is the single source of truth for side, machine and remaining
portions.
"""

from .errors import (
    LoomConflict,
    LoomError,
    LoomNotFound,
    LoomValidationError,
)
from .planner import (
    MAX_ROWS,
    MAX_SHUTTLES,
    MAX_TICKETS,
    LEFT,
    RIGHT,
    ShuttleSnapshot,
    plan_tape,
)
from .schema import LOOM_SCHEMA
from .service import LoomService

__all__ = [
    "LoomService",
    "LoomError",
    "LoomValidationError",
    "LoomConflict",
    "LoomNotFound",
    "ShuttleSnapshot",
    "plan_tape",
    "LOOM_SCHEMA",
    "LEFT",
    "RIGHT",
    "MAX_SHUTTLES",
    "MAX_ROWS",
    "MAX_TICKETS",
]

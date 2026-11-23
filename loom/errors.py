"""Exception hierarchy for the weaving room."""

from __future__ import annotations


class LoomError(Exception):
    """Base class for all loom domain errors."""

    error_type = "LoomError"


class LoomValidationError(LoomError):
    error_type = "LoomValidationError"


class LoomNotFound(LoomError):
    error_type = "LoomNotFound"


class LoomConflict(LoomError):
    """Request conflicts with current physical or job state."""

    error_type = "LoomConflict"

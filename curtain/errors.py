"""Exception types for the curtain measurement / arrangement service."""

from __future__ import annotations

from reagent.errors import ReagentError


class CurtainError(ReagentError):
    """Base class for curtain-domain errors."""

    error_type = "CurtainError"


class InfeasibleRecords(CurtainError):
    """The countersigned record set admits no joint interpretation."""

    error_type = "InfeasibleRecords"

    def __init__(self, message: str, core: list[str] | None = None):
        super().__init__(message)
        self.core = list(core or [])


class NoArrangement(CurtainError):
    """No placement of the permitted spares satisfies every seam."""

    error_type = "NoArrangement"

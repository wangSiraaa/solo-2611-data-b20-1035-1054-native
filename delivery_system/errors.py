"""Domain errors with actionable messages."""

from __future__ import annotations

from typing import Optional


class DeliveryError(Exception):
    """Base class for expected domain failures."""


class DocumentExists(DeliveryError):
    pass


class DocumentNotFound(DeliveryError):
    pass


class SegmentNotFound(DeliveryError):
    pass


class DuplicateSegment(DeliveryError):
    pass


class InvalidPosition(DeliveryError):
    pass


class RevisionConflict(DeliveryError):
    pass


class TranslationExists(DeliveryError):
    pass


class InvalidTranslation(DeliveryError):
    pass


class PackageExistsImmutable(DeliveryError):
    """A package id was reused with different frozen content."""


class NoCoverError(DeliveryError):
    """Publishing was attempted but no exact current cover exists."""

    def __init__(self, message: str, result: Optional[object] = None) -> None:
        super().__init__(message)
        self.result = result


class InvalidFoldPlan(DeliveryError):
    """A strip/fold diagram is malformed or inconsistent."""


class InvalidMold(DeliveryError):
    """A needle mold or geometry is malformed or does not fit the strip."""


class CalibrationContradiction(DeliveryError):
    """The calibration sheets cannot be explained by any single setting."""


class HandoffError(DeliveryError):
    """A calibration handoff package is malformed or fails its own checks."""

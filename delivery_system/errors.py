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


# ----------------------------------------------------------------------
# Collation / character-order investigation
# ----------------------------------------------------------------------
class CollationError(DeliveryError):
    """Base class for vocabulary-list / character-order investigation failures.

    ``reason`` is a short, stable machine-readable code (see subclasses) so the
    cataloguer can act on a definite rejection without parsing the message.
    """

    reason = "collation_error"

    def __init__(self, message: str, detail: Optional[object] = None) -> None:
        super().__init__(message)
        self.detail = detail


class IntervalOutOfBounds(CollationError):
    """A headword interval does not fit inside its translation block."""

    reason = "interval_out_of_bounds"


class Untokenizable(CollationError):
    """A headword cannot be split completely with the local type matrix."""

    reason = "untokenizable"


class StalePackage(CollationError):
    """An old translation block was pasted onto a different (newer) edition."""

    reason = "stale_package"


class UnknownBlock(CollationError):
    """A headword mark names a translation block absent from the package."""

    reason = "unknown_block"


class MalformedMark(CollationError):
    """Overlapping marks, duplicate entry numbers, or empty intervals."""

    reason = "malformed_mark"


class InvalidBook(CollationError):
    """The new book spec is internally inconsistent (ids, numbers, capacity)."""

    reason = "invalid_book"


class UnknownEntry(CollationError):
    """A loose-page clue or query names an entry absent from the new book."""

    reason = "unknown_entry"


class TooManyOrders(CollationError):
    """More compatible total orders exist than the configured enumeration cap.

    The pre-investigated precondition promises at most ~20000 compatible total
    orders; hitting this means the query is outside that promised regime.
    """

    reason = "too_many_orders"

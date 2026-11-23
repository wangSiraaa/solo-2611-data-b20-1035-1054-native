"""Domain errors for the crossword layer."""

from __future__ import annotations

from ..errors import DeliveryError


class CrosswordError(DeliveryError):
    """Base class for expected crossword-layer failures."""


class ClueNotFound(CrosswordError):
    pass


class ClueExists(CrosswordError):
    pass


class RootNotFound(CrosswordError):
    pass


class SuffixClassNotFound(CrosswordError):
    pass


class SenseRootMismatch(CrosswordError):
    """A coinage used a root that does not belong to the clue's sense."""


class SuffixNotAllowed(CrosswordError):
    """A suffix is not in any of the root's registered suffix classes."""


class BannedCombination(CrosswordError):
    """A root+suffix combination is on the audited ban list."""


class CoinageCoverageError(CrosswordError):
    """A sheet does not cover exactly the clue(s) of its block, or duplicates."""


class SheetImmutable(CrosswordError):
    """A sheet id was reused with different content."""


class SheetBlockMismatch(CrosswordError):
    """A sheet was applied to a different package/block than it was cut for."""


class NoCredential(CrosswordError):
    """No valid credential exists for a requested spelling."""


class HandoffModeError(CrosswordError):
    pass


class RuleError(CrosswordError):
    pass

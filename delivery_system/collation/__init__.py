"""Lead-type character-order (字序) investigation on top of delivered packages.

This subpackage turns *actually delivered* ``translation-package/1`` blocks into
ordered, numbered headwords (词头), then answers two kinds of question:

* **Investigation (调查):** which strict total orders over the complete local
  type matrix make the given old word list(s) sort correctly?  A proposed order
  can be checked, and a contradiction is pinned to a concrete headword pair and
  edition.
* **Binding (装订):** given the new book's entries, its per-fascicle capacity and
  the fragments recovered from loose pages, where (fascicle, position) can a
  queried entry genuinely appear across all whole books compatible with the
  investigation and *every* loose-page clue?  Each possible position carries a
  witness full-book glyph order so the book order can be rebuilt.

Nothing here re-implements packaging: headwords are read out of real package
text using proofreader-marked character intervals and source revisions.
"""

from .book import (
    AdjacentClue,
    BookEntry,
    BookQueryResult,
    BookSolver,
    ConflictExplanation,
    NewBook,
    PositionClue,
    QueryPossiblePosition,
)
from .glyphs import (
    GlyphOrder,
    TypeMatrix,
    compare_headwords,
    compare_tokens,
    entry_id_key,
    headword_sort_key,
)
from .handoff import (
    CollationSlip,
    LocalConclusion,
    PrecedenceCertificate,
    PrecedenceResult,
    RevocationResult,
    build_slip,
    certify_precedence,
    match_clues_against_slip,
    reconstruct_book_orders,
    revoke_on_new_clue,
    viable_conclusions,
)
from .investigation import (
    CheckResult,
    CheckViolation,
    CollationInvestigation,
    ConstraintContradiction,
    GlyphConstraint,
    InvestigationResult,
)
from .marks import (
    DeliveryBlock,
    Headword,
    HeadwordMark,
    WordList,
    blocks_from_package,
    build_headwords,
    build_wordlist,
)

__all__ = [
    "AdjacentClue",
    "BookEntry",
    "BookQueryResult",
    "BookSolver",
    "CheckResult",
    "CheckViolation",
    "CollationInvestigation",
    "CollationSlip",
    "ConflictExplanation",
    "ConstraintContradiction",
    "DeliveryBlock",
    "GlyphConstraint",
    "GlyphOrder",
    "Headword",
    "HeadwordMark",
    "InvestigationResult",
    "LocalConclusion",
    "NewBook",
    "PositionClue",
    "PrecedenceCertificate",
    "PrecedenceResult",
    "QueryPossiblePosition",
    "RevocationResult",
    "TypeMatrix",
    "WordList",
    "blocks_from_package",
    "build_headwords",
    "build_slip",
    "build_wordlist",
    "certify_precedence",
    "compare_headwords",
    "compare_tokens",
    "entry_id_key",
    "headword_sort_key",
    "match_clues_against_slip",
    "reconstruct_book_orders",
    "revoke_on_new_clue",
    "viable_conclusions",
]

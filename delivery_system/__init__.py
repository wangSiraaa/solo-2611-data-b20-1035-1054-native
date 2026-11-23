"""Source-segment catalog + multilingual delivery repository.

Two deliberately separate sides:

* :class:`delivery_system.source_catalog.SourceCatalog` owns documents,
  stable segment identities, their order, current revisions and text.  It
  exposes an immutable :class:`SourceProduct` snapshot.
* :class:`delivery_system.delivery_repository.DeliveryRepository` owns
  translation entries and published packages.  It reads the *published source
  product* from the catalog and solves an exact, non-overlapping cover for one
  language at a time.

No translation or semantic scoring happens here: text is opaque.
"""

from .coverage import solve_cover
from .delivery_repository import DeliveryRepository
from .models import (
    CoverChunk,
    CoverDiagnosis,
    CoverResult,
    RuleVocabulary,
    SourceProduct,
    SourceRef,
    SourceSegment,
    TranslationAssessment,
    TranslationEntry,
)
from .plan import (
    PlanResult,
    build_handoff,
    run_script,
    screen_candidates,
    synthesize,
    synthesize_merged,
    verify_plan,
)
from .rules import (
    Clause,
    Machine,
    Volume,
    check_completeness,
    replay,
    step,
)
from .source_catalog import SourceCatalog

__all__ = [
    "Clause",
    "CoverChunk",
    "CoverDiagnosis",
    "CoverResult",
    "DeliveryRepository",
    "Machine",
    "PlanResult",
    "RuleVocabulary",
    "SourceCatalog",
    "SourceProduct",
    "SourceRef",
    "SourceSegment",
    "TranslationAssessment",
    "TranslationEntry",
    "Volume",
    "build_handoff",
    "check_completeness",
    "replay",
    "run_script",
    "screen_candidates",
    "solve_cover",
    "step",
    "synthesize",
    "synthesize_merged",
    "verify_plan",
]

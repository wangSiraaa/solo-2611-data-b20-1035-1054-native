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
from .fold_operations import OperationMark, OperationMarkAssessment, assess_operation_mark
from .folding import (
    FoldPlan,
    FoldVoucher,
    Layer,
    State,
    StripVersion,
    fold_state,
    initial_state,
)
from .handoff import (
    CalibrationHandoff,
    HandoffResult,
    build_handoff,
    handoff_from_dict,
    replay_order,
)
from .models import (
    CoverChunk,
    CoverDiagnosis,
    CoverResult,
    SourceProduct,
    SourceRef,
    SourceSegment,
    TranslationAssessment,
    TranslationEntry,
)
from .needle_mold import (
    INTACT,
    PUNCHED,
    NeedleMold,
    OrderJudgment,
    OrderRequirement,
    PunchSheet,
    WorkOrder,
    compatible_settings,
    judge_order,
    judge_work_order,
    unfold_pattern,
)
from .source_catalog import SourceCatalog

__all__ = [
    "CalibrationHandoff",
    "CoverChunk",
    "CoverDiagnosis",
    "CoverResult",
    "DeliveryRepository",
    "FoldPlan",
    "FoldVoucher",
    "INTACT",
    "Layer",
    "OperationMark",
    "OperationMarkAssessment",
    "OrderJudgment",
    "OrderRequirement",
    "PUNCHED",
    "PunchSheet",
    "SourceCatalog",
    "SourceProduct",
    "SourceRef",
    "SourceSegment",
    "State",
    "StripVersion",
    "TranslationAssessment",
    "TranslationEntry",
    "WorkOrder",
    "assess_operation_mark",
    "build_handoff",
    "compatible_settings",
    "fold_state",
    "handoff_from_dict",
    "initial_state",
    "judge_order",
    "judge_work_order",
    "replay_order",
    "solve_cover",
    "unfold_pattern",
]

"""gradaudit -- independent course-equivalence catalog and graduation audit library.

Public entry points
-------------------
* :func:`verify_plan_document` -- independent ``verify-plan`` (no solver).
* :func:`gradaudit.solver.solve` -- exact feasibility solver.
* :class:`Store` -- SQLite-backed catalog / records / requirements / audit
  preparation-confirmation library.
* :func:`gradaudit.exchange.check_compatibility` -- universal, exhaustive
  directed-channel compatibility testing with witnesses; :class:`CompatibilityForm`
  / :class:`CompatibilityStore` for signed forms.
* :func:`gradaudit.scheduler.schedule` -- exact switch calendar over directed
  channels using signed forms, night availability and per-night capacity.

Typical CLI use::

    python -m gradaudit import-catalog catalog.json
    python -m gradaudit import-records records.json
    python -m gradaudit import-requirements requirements.json
    python -m gradaudit candidates X
    python -m gradaudit solve --catalog ... --records ... --requirements ...
    python -m gradaudit verify --plan plan_a.json --catalog ... --records ... --requirements ...
    python -m gradaudit prepare --plan ... --request REQ1 ...
    python -m gradaudit confirm --request REQ1
    python -m gradaudit status --request REQ1
    python -m gradaudit reopen --request REQ1 --reason "..."
    python -m gradaudit history --request REQ1
"""

from .errors import (
    ConfirmationError,
    ConfirmationStaleError,
    GradAuditError,
    NotPreparedError,
    RevisionConflictError,
    RevisionNotFoundError,
    SchemaError,
    VerificationError,
)
from .library import Store
from .model import (
    Assignment,
    Catalog,
    Plan,
    Record,
    Records,
    Requirements,
    content_hash,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
)
from .solver import MasterResult, GroupOutcome, solve, single_group_max
from .verify import VerifyResult, verify_plan, verify_plan_document
from .exchange import (
    CompatibilityForm,
    CompatibilityResult,
    CompatibilityStore,
    RequirementOutcome,
    Scope,
    audit_scope,
    check_compatibility,
    enumerate_transcripts,
    parse_scope,
    records_for_transcript,
    sign_channel_forms,
    sign_form,
    verify_witness,
)
from .scheduler import (
    Booklet,
    CalendarResult,
    Channel,
    EdgeConstraint,
    NightBatch,
    School,
    SwitchProblem,
    derive_constraints,
    parse_switch_problem,
    schedule,
)

__all__ = [
    "Store",
    "solve",
    "single_group_max",
    "MasterResult",
    "GroupOutcome",
    "verify_plan",
    "verify_plan_document",
    "VerifyResult",
    "Catalog",
    "Records",
    "Requirements",
    "Plan",
    "Record",
    "Assignment",
    "parse_catalog",
    "parse_records",
    "parse_requirements",
    "parse_plan",
    "content_hash",
    "GradAuditError",
    "SchemaError",
    "RevisionNotFoundError",
    "RevisionConflictError",
    "VerificationError",
    "NotPreparedError",
    "ConfirmationError",
    "ConfirmationStaleError",
    # exchange compatibility lab
    "Scope",
    "parse_scope",
    "CompatibilityResult",
    "RequirementOutcome",
    "CompatibilityForm",
    "CompatibilityStore",
    "check_compatibility",
    "audit_scope",
    "enumerate_transcripts",
    "records_for_transcript",
    "sign_form",
    "sign_channel_forms",
    "verify_witness",
    # switch scheduling
    "Booklet",
    "CalendarResult",
    "Channel",
    "EdgeConstraint",
    "NightBatch",
    "School",
    "SwitchProblem",
    "derive_constraints",
    "parse_switch_problem",
    "schedule",
]

__version__ = "1.0.0"

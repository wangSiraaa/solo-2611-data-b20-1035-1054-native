"""gradaudit -- independent course-equivalence catalog and graduation audit library.

Public entry points
-------------------
* :func:`verify_plan_document` -- independent ``verify-plan`` (no solver).
* :func:`gradaudit.solver.solve` -- exact feasibility solver.
* :class:`Store` -- SQLite-backed catalog / records / requirements / audit
  preparation-confirmation library.

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
    CounterStateError,
    GradAuditError,
    NotPreparedError,
    PackageError,
    PressError,
    RevisionConflictError,
    RevisionNotFoundError,
    SchemaError,
    SheetError,
    TermError,
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

# certificate-issuance subsystem (registry sheet / terminology / press / counter)
from .sheet import MasterSheet, SheetRow, build_master_sheet
from .terms import TermOffice, TermPair, check_naming, explain_naming, solve_namings
from .press import (
    POINTS_CHANNEL,
    FormSpec,
    PressFailure,
    PressPlan,
    RecipientSpec,
    check_distribution,
    plan_press,
    simulate_press,
)
from .counter import Counter, RegistryOffice, office_from_permit

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
    "TermError",
    "SheetError",
    "PressError",
    "PackageError",
    "CounterStateError",
    # issuance subsystem
    "MasterSheet",
    "SheetRow",
    "build_master_sheet",
    "TermOffice",
    "TermPair",
    "check_naming",
    "solve_namings",
    "explain_naming",
    "POINTS_CHANNEL",
    "FormSpec",
    "RecipientSpec",
    "PressPlan",
    "PressFailure",
    "simulate_press",
    "check_distribution",
    "plan_press",
    "RegistryOffice",
    "Counter",
    "office_from_permit",
]

__version__ = "1.0.0"

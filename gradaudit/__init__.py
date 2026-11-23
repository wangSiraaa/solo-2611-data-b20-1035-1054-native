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

from .billing import (
    BILL_FORMAT,
    BILLING_POLICY_FORMAT,
    BillingPolicy,
    RecordCharge,
    SemesterBill,
    Tier,
    bill_account_delta,
    bill_body,
    parse_billing_policy,
    price_semester,
    sign_bill_document,
    verify_bill_signature,
)
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
from .bank import (
    BankError,
    ExecutionResult,
    LocalBankCounter,
    Receipt,
    ResponseLost,
    SettlementDesk,
)
from .reconcile import (
    AccountBounds,
    BankFlow,
    ReconciliationPlan,
    account_bounds,
    parse_caps,
    parse_flows,
    plan_reconciliation,
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
    # billing
    "BillingPolicy",
    "RecordCharge",
    "SemesterBill",
    "Tier",
    "parse_billing_policy",
    "price_semester",
    "bill_body",
    "sign_bill_document",
    "verify_bill_signature",
    "bill_account_delta",
    "BILL_FORMAT",
    "BILLING_POLICY_FORMAT",
    # reconciliation
    "AccountBounds",
    "BankFlow",
    "ReconciliationPlan",
    "account_bounds",
    "parse_caps",
    "parse_flows",
    "plan_reconciliation",
    # bank counter
    "BankError",
    "ExecutionResult",
    "LocalBankCounter",
    "Receipt",
    "ResponseLost",
    "SettlementDesk",
]

__version__ = "1.0.0"

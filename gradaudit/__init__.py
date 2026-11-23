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
    GradAuditError,
    HandoverError,
    LedgerError,
    NotPreparedError,
    ProtocolError,
    RevisionConflictError,
    RevisionNotFoundError,
    SchemaError,
    SuspensionError,
    VerificationError,
    WindowError,
)
from .handover import (
    CoverageReceipt,
    FaceJudgement,
    HandoverPacket,
    Roster,
    build_packet,
    compare_ledger_and_roster,
    coverage_receipt,
    diff_judgements,
    ledger_judgements,
    multi_issue_thresholds,
    parse_roster,
    review_packet,
    roster_from_ledgers,
)
from .ledger import Ledger, LineItem, Pool, build_ledger
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
from .protocol import Protocol, ProtocolEntry, parse_protocol
from .rationals import format_ratio, parse_positive_rational
from .solver import MasterResult, GroupOutcome, solve, single_group_max
from .verify import VerifyResult, verify_plan, verify_plan_document
from .windows import (
    Network,
    Node,
    ProfitFinding,
    StartState,
    SuspensionRequest,
    SuspensionResult,
    Walk,
    Window,
    find_multi_issue,
    find_profits,
    parse_network,
    parse_suspension_request,
    reachable,
    solve_suspension,
    stamp,
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
    "ProtocolError",
    "LedgerError",
    "WindowError",
    "SuspensionError",
    "HandoverError",
    # credit protocol + ledger
    "Protocol",
    "ProtocolEntry",
    "parse_protocol",
    "Ledger",
    "Pool",
    "LineItem",
    "build_ledger",
    "parse_positive_rational",
    "format_ratio",
    # exchange windows + suspension
    "Network",
    "Node",
    "Window",
    "Walk",
    "StartState",
    "ProfitFinding",
    "SuspensionRequest",
    "SuspensionResult",
    "parse_network",
    "parse_suspension_request",
    "stamp",
    "find_multi_issue",
    "find_profits",
    "reachable",
    "solve_suspension",
    # offline handover
    "Roster",
    "FaceJudgement",
    "CoverageReceipt",
    "HandoverPacket",
    "parse_roster",
    "roster_from_ledgers",
    "coverage_receipt",
    "ledger_judgements",
    "compare_ledger_and_roster",
    "multi_issue_thresholds",
    "diff_judgements",
    "build_packet",
    "review_packet",
]

__version__ = "1.0.0"

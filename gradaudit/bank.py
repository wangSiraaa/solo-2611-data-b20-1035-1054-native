"""Re-openable local bank counter and settlement execution.

The counter is the *bank's* durable ledger.  It is deliberately a separate
object from the operator's desk so that:

* a reversal only counts once the **bank** has applied it -- writing a local
  negative number at the operator desk never pretends the bank refunded
  (``不能在经办处记上一条本地反向数就当银行已经退款``);
* it can be **re-opened independently** (new instance over the same DB) and a
  caller can look a transaction up by the same bank reference or re-issue it;
* receipt packets may be lost, yet every operation is idempotent by
  ``operation_id`` so a retry after a lost response never double-applies.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .errors import GradAuditError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResponseLost(GradAuditError):
    """The bank applied the operation but its receipt packet was lost."""

    code = "response_lost"


class BankError(GradAuditError):
    code = "bank_error"


@dataclass(frozen=True)
class Receipt:
    operation_id: str
    kind: str                 # "reverse" | "debit"
    reference: str            # reversal uses the flow reference; debit a fresh one
    account: Optional[str]
    amount: int
    status: str               # "applied" | "already_applied"
    at: str


class LocalBankCounter:
    """A bank-side ledger that survives re-opening.

    Tables::

        bank_operations(operation_id PK, kind, reference, account, amount,
                        status, bank_ref, applied_at)
        bank_reversal_index(reference PK, operation_id)   -- flow -> reversal
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bank_operations (
                operation_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                reference TEXT NOT NULL,
                account TEXT,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                bank_ref TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bank_reversal_index (
                reference TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LocalBankCounter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- query ------------------------------------------------------------
    def query(self, operation_id: str) -> Optional[Receipt]:
        row = self.conn.execute(
            "SELECT * FROM bank_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        return self._receipt(row) if row else None

    def query_reference(self, reference: str) -> List[Receipt]:
        rows = self.conn.execute(
            "SELECT * FROM bank_operations WHERE reference = ? ORDER BY applied_at, rowid",
            (reference,),
        ).fetchall()
        return [self._receipt(r) for r in rows]

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Receipt:
        return Receipt(
            operation_id=row["operation_id"],
            kind=row["kind"],
            reference=row["reference"],
            account=row["account"],
            amount=row["amount"],
            status=row["status"],
            at=row["applied_at"],
        )

    # -- apply ------------------------------------------------------------
    def _insert_op(
        self, operation_id: str, kind: str, reference: str, account: Optional[str], amount: int
    ) -> Tuple[Receipt, bool]:
        existing = self.query(operation_id)
        if existing is not None:
            # A replay by the same operation id is a no-op; report it as such so
            # callers can tell a fresh application from an idempotent re-apply.
            return Receipt(
                operation_id=existing.operation_id,
                kind=existing.kind,
                reference=existing.reference,
                account=existing.account,
                amount=existing.amount,
                status="already_applied",
                at=existing.at,
            ), False
        bank_ref = f"BANK-{kind.upper()}-{operation_id}"
        at = _now()
        self.conn.execute(
            "INSERT INTO bank_operations(operation_id, kind, reference, account, amount, status, bank_ref, applied_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (operation_id, kind, reference, account, amount, "applied", bank_ref, at),
        )
        if kind == "reverse":
            self.conn.execute(
                "INSERT OR IGNORE INTO bank_reversal_index(reference, operation_id) VALUES (?,?)",
                (reference, operation_id),
            )
        self.conn.commit()
        return Receipt(operation_id, kind, reference, account, amount, "applied", at), True

    def reverse(self, reference: str, *, operation_id: str, lose_response: bool = False) -> Receipt:
        """Reverse a whole incoming flow by its bank reference (idempotent)."""
        receipt, applied = self._insert_op(operation_id, "reverse", reference, None, 0)
        if applied and lose_response:
            raise ResponseLost(
                f"reversal {reference!r} applied but its receipt packet was lost",
                location=reference,
                detail={"operation_id": operation_id},
            )
        return receipt if not applied else receipt

    def debit(
        self,
        account: str,
        amount: int,
        *,
        reference: str,
        operation_id: str,
        lose_response: bool = False,
    ) -> Receipt:
        """Debit ``amount`` from ``account`` under a supplementary authorization."""
        receipt, applied = self._insert_op(operation_id, "debit", reference, account, amount)
        if applied and lose_response:
            raise ResponseLost(
                f"debit {operation_id!r} applied but its receipt packet was lost",
                location=account,
                detail={"operation_id": operation_id},
            )
        return receipt

    def reissue(self, operation_id: str) -> Receipt:
        """Re-query an operation by the same identity and return its real receipt.

        This is the "true re-issue": it does not create a second movement, it
        returns the bank's own record so the operator can recover from a lost
        response without guessing.
        """
        receipt = self.query(operation_id)
        if receipt is None:
            raise BankError(f"operation {operation_id!r} is unknown to the bank", location=operation_id)
        return Receipt(
            operation_id=receipt.operation_id,
            kind=receipt.kind,
            reference=receipt.reference,
            account=receipt.account,
            amount=receipt.amount,
            status="already_applied",
            at=receipt.at,
        )


# --------------------------------------------------------------------------- #
# operator desk: executes an already-decided plan, never invents movements
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExecutionResult:
    executed: bool
    actions: Tuple[dict, ...]
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {"executed": self.executed, "actions": [dict(a) for a in self.actions], "reason": self.reason}


class SettlementDesk:
    """Applies a *pre-decided* :class:`~gradaudit.reconcile.ReconciliationPlan`.

    The plan is fixed before any bank call.  If a response packet is lost the
    desk re-queries the **same** operation id (re-issue) and completes only what
    the fixed plan calls for -- it never selects a different plan mid-flight.
    """

    def __init__(self, counter: LocalBankCounter, *, lose_responses: bool = False):
        self.counter = counter
        self.lose_responses = lose_responses

    def execute(self, plan, *, scope: str = "batch") -> ExecutionResult:
        if not plan.feasible:
            # Infeasible => zero actions, keep the bank exactly as it was.
            return ExecutionResult(executed=False, actions=(), reason=plan.reason)

        actions = []
        for ref in plan.reversed_refs:
            op_id = f"REV-{scope}-{ref}"
            receipt = self._apply(
                lambda: self.counter.reverse(ref, operation_id=op_id, lose_response=self.lose_responses),
                op_id,
            )
            actions.append(
                {
                    "kind": "reverse",
                    "reference": ref,
                    "operation_id": op_id,
                    "status": receipt.status,
                    "bank_ref": f"BANK-REVERSE-{op_id}",
                }
            )

        for account, amount in sorted(plan.debits.items()):
            if amount <= 0:
                continue
            op_id = f"DEB-{scope}-{account}"
            ref = f"SUPP-{scope}-{account}"
            receipt = self._apply(
                lambda account=account, amount=amount, ref=ref, op_id=op_id: self.counter.debit(
                    account, amount, reference=ref, operation_id=op_id, lose_response=self.lose_responses
                ),
                op_id,
            )
            actions.append(
                {
                    "kind": "debit",
                    "account": account,
                    "amount": amount,
                    "reference": ref,
                    "operation_id": op_id,
                    "status": receipt.status,
                    "bank_ref": f"BANK-DEBIT-{op_id}",
                }
            )

        return ExecutionResult(executed=True, actions=tuple(actions))

    def _apply(self, call, op_id: str) -> Receipt:
        try:
            return call()
        except ResponseLost:
            # Recover via a true re-issue by the same operation id.
            return self.counter.reissue(op_id)


__all__ = [
    "ResponseLost",
    "BankError",
    "Receipt",
    "LocalBankCounter",
    "SettlementDesk",
    "ExecutionResult",
]

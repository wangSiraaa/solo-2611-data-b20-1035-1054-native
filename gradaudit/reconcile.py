"""Bank reconciliation for a semester bill.

Given the *new* per-account receivable ``R`` (from a signed semester bill), the
flows the bank has already received, and each account's debit-authorization cap,
work out which old flows to reverse in full and how much to debit per account so
that **every account's final received amount equals ``R`` exactly**.

Rules honoured here
-------------------
* A flow is an indivisible bank transaction: reversing it returns *all* of its
  per-account amounts (``银行只能整笔原路冲回，不能只冲其中一个账户``).  It is
  never possible to reverse just one account's share.
* For account ``a`` with current received ``cur[a]``, receivable ``R[a]`` and
  cap ``cap[a]``, reversing a total ``reversed[a]`` implies a supplementary
  debit ``d[a] = R[a] - cur[a] + reversed[a]``.  Both ``d[a] >= 0`` and
  ``d[a] <= cap[a]`` must hold, i.e.

      L[a] = max(0, cur[a] - R[a])          <=  reversed[a]
      reversed[a] <= cur[a] + cap[a] - R[a]  =  U[a]

  If ``L[a] > U[a]`` no plan exists for that account at all.
* Among all feasible subsets we pick one with the **fewest reversed flows**
  (``挑尽量少的旧流水整笔冲回``).  Ties are broken deterministically and any tie
  is acceptable (``并列方案任选``).
* If no feasible subset exists the result is infeasible and the caller must
  perform **zero** bank actions (``办不到就保留原状``).

Note that reversing *more* flow value always *increases* the required debit, so
the ``U`` bound is what a naive "reverse the biggest flow that clears the
overpayment" heuristic trips over: a flow can clear one account's overpayment
yet push another account's debit past its cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import SchemaError

FLOWS_FORMAT = "bank-flows/1"
CAPS_FORMAT = "debit-authorizations/1"
PLAN_FORMAT = "bank-reconciliation/1"

MAX_FLOWS = 12
MAX_ACCOUNTS = 6


def _require(cond: bool, message: str, *, location: Optional[str] = None) -> None:
    if not cond:
        raise SchemaError(message, location=location)


def _as_amount(value: Any, *, name: str, location: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{name} must be an integer",
        location=location,
    )
    _require(value >= 0, f"{name} must be non-negative", location=location)
    return value


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BankFlow:
    reference: str
    amounts: Dict[str, int]   # account -> amount (>=0)

    def as_dict(self) -> dict:
        return {"reference": self.reference, "amounts": dict(self.amounts)}


@dataclass(frozen=True)
class AccountBounds:
    account_id: str
    current: int
    receivable: int
    cap: int
    min_reversal: int      # L
    max_reversal: int      # U
    impossible: bool       # L > U

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "current": self.current,
            "receivable": self.receivable,
            "cap": self.cap,
            "min_reversal": self.min_reversal,
            "max_reversal": self.max_reversal,
            "impossible": self.impossible,
        }


@dataclass(frozen=True)
class ReconciliationPlan:
    feasible: bool
    reversed_refs: Tuple[str, ...]
    rejected_refs: Tuple[str, ...]
    debits: Dict[str, int]
    final_balances: Dict[str, int]
    reversed_amounts: Dict[str, int]
    bounds: Tuple[AccountBounds, ...]
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "format": PLAN_FORMAT,
            "feasible": self.feasible,
            "reversed_refs": list(self.reversed_refs),
            "kept_refs": list(self.rejected_refs),
            "debits": {k: self.debits[k] for k in sorted(self.debits)},
            "final_balances": {k: self.final_balances[k] for k in sorted(self.final_balances)},
            "reversed_amounts": {k: self.reversed_amounts[k] for k in sorted(self.reversed_amounts)},
            "bounds": [b.as_dict() for b in self.bounds],
            "reason": self.reason,
        }


def parse_flows(doc: Any) -> List[BankFlow]:
    _require(isinstance(doc, dict), "bank flows document must be a JSON object")
    _require(
        doc.get("format") == FLOWS_FORMAT,
        f"bank flows format must be {FLOWS_FORMAT!r}",
        location="format",
    )
    raw = doc.get("flows")
    _require(isinstance(raw, list), "flows must be a list", location="flows")
    _require(len(raw) <= MAX_FLOWS, f"at most {MAX_FLOWS} flows are supported", location="flows")
    seen = set()
    accounts = set()
    flows = []
    for idx, entry in enumerate(raw):
        loc = f"flows[{idx}]"
        _require(isinstance(entry, dict), "flow must be an object", location=loc)
        ref = entry.get("reference")
        _require(
            isinstance(ref, str) and ref.strip() != "",
            "flow reference must be a non-empty string",
            location=loc,
        )
        _require(ref not in seen, f"duplicate flow reference {ref!r}", location=ref)
        seen.add(ref)
        amounts = entry.get("amounts")
        _require(isinstance(amounts, dict) and amounts, f"flow {ref!r} amounts must be a non-empty object", location=ref)
        norm = {}
        for acct, amt in amounts.items():
            _require(isinstance(acct, str) and acct.strip() != "", "account id must be a non-empty string", location=ref)
            norm[acct] = _as_amount(amt, name="amount", location=f"{ref}.{acct}")
            accounts.add(acct)
        flows.append(BankFlow(reference=ref, amounts=norm))
    _require(len(accounts) <= MAX_ACCOUNTS, f"at most {MAX_ACCOUNTS} accounts are supported", location="flows")
    # deterministic order (by reference) so ties resolve stably
    flows.sort(key=lambda f: f.reference)
    return flows


def parse_caps(doc: Any) -> Dict[str, int]:
    _require(isinstance(doc, dict), "debit-authorization document must be a JSON object")
    _require(
        doc.get("format") == CAPS_FORMAT,
        f"debit-authorization format must be {CAPS_FORMAT!r}",
        location="format",
    )
    raw = doc.get("caps")
    _require(isinstance(raw, dict), "caps must be an object", location="caps")
    caps = {}
    for acct, amt in raw.items():
        _require(isinstance(acct, str) and acct.strip() != "", "account id must be a non-empty string", location="caps")
        caps[acct] = _as_amount(amt, name="cap", location=f"caps.{acct}")
    _require(len(caps) <= MAX_ACCOUNTS, f"at most {MAX_ACCOUNTS} accounts are supported", location="caps")
    return caps


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #
def account_bounds(
    receivable: Mapping[str, int],
    flows: Sequence[BankFlow],
    caps: Mapping[str, int],
) -> List[AccountBounds]:
    """Per-account ``[L, U]`` bounds on how much flow value must be reversed.

    This is the "boundary-first" view: it tells an operator, before touching any
    flow, exactly how much each account must give back and how much it may give
    back without exhausting its debit authorization.
    """
    accounts = set(receivable) | set(caps)
    for flow in flows:
        accounts |= set(flow.amounts)
    current: Dict[str, int] = {a: 0 for a in accounts}
    for flow in flows:
        for acct, amt in flow.amounts.items():
            current[acct] += amt

    out = []
    for acct in sorted(accounts):
        cur = current.get(acct, 0)
        recv = int(receivable.get(acct, 0))
        cap = int(caps.get(acct, 0))
        L = max(0, cur - recv)
        U = cur + cap - recv
        out.append(
            AccountBounds(
                account_id=acct,
                current=cur,
                receivable=recv,
                cap=cap,
                min_reversal=L,
                max_reversal=U,
                impossible=L > U,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
def plan_reconciliation(
    receivable: Mapping[str, int],
    flows: Sequence[BankFlow],
    caps: Mapping[str, int],
) -> ReconciliationPlan:
    """Minimum-reversal feasible plan, or an infeasible (zero-action) result."""
    _require(len(flows) <= MAX_FLOWS, f"at most {MAX_FLOWS} flows are supported", location="flows")
    # Stable total order so tie-breaking (away from the minimum-count objective)
    # is deterministic no matter how the caller built the list.
    flows = sorted(flows, key=lambda f: f.reference)
    bounds = account_bounds(receivable, flows, caps)
    accounts = [b.account_id for b in bounds]
    receivable = {a: int(receivable.get(a, 0)) for a in accounts}
    caps = {a: int(caps.get(a, 0)) for a in accounts}
    current = {b.account_id: b.current for b in bounds}
    low = {b.account_id: b.min_reversal for b in bounds}
    high = {b.account_id: b.max_reversal for b in bounds}

    for b in bounds:
        if b.impossible:
            return ReconciliationPlan(
                feasible=False,
                reversed_refs=(),
                rejected_refs=tuple(f.reference for f in flows),
                debits={},
                final_balances={},
                reversed_amounts={},
                bounds=tuple(bounds),
                reason=(
                    f"account {b.account_id!r} needs a debit of "
                    f"{b.receivable - b.current} but its authorization cap is {b.cap}"
                    if b.receivable - b.current > b.cap
                    else f"account {b.account_id!r} has reversal bounds L={b.min_reversal} > U={b.max_reversal}"
                ),
            )

    # Flows are already sorted by reference for deterministic tie-breaking.
    n = len(flows)
    best: Optional[Tuple[int, Tuple[str, ...], Dict[str, int]]] = None
    # Enumerate by increasing reversal count to guarantee "fewest flows".
    from itertools import combinations

    indices = list(range(n))
    for k in range(0, n + 1):
        for combo in combinations(indices, k):
            rev = {a: 0 for a in accounts}
            for i in combo:
                for acct, amt in flows[i].amounts.items():
                    rev[acct] += amt
            if all(low[a] <= rev[a] <= high[a] for a in accounts):
                refs = tuple(flows[i].reference for i in combo)
                best = (k, refs, rev)
                break
        if best is not None:
            break

    if best is None:
        return ReconciliationPlan(
            feasible=False,
            reversed_refs=(),
            rejected_refs=tuple(f.reference for f in flows),
            debits={},
            final_balances={},
            reversed_amounts={},
            bounds=tuple(bounds),
            reason="no combination of whole-flow reversals fits every account's debit-authorization cap",
        )

    _, refs, rev = best
    reversed_set = set(refs)
    kept = tuple(f.reference for f in flows if f.reference not in reversed_set)
    debits = {a: receivable[a] - current[a] + rev[a] for a in accounts}
    final = {a: current[a] - rev[a] + debits[a] for a in accounts}
    # Guard: the plan must land exactly on the receivable.
    for a in accounts:
        if final[a] != receivable[a] or debits[a] < 0 or debits[a] > caps[a]:
            return ReconciliationPlan(
                feasible=False,
                reversed_refs=(),
                rejected_refs=tuple(f.reference for f in flows),
                debits={},
                final_balances={},
                reversed_amounts={},
                bounds=tuple(bounds),
                reason=f"internal invariant violated for account {a!r}",
            )

    return ReconciliationPlan(
        feasible=True,
        reversed_refs=refs,
        rejected_refs=kept,
        debits=debits,
        final_balances=final,
        reversed_amounts={a: rev[a] for a in accounts},
        bounds=tuple(bounds),
        reason=None,
    )


__all__ = [
    "FLOWS_FORMAT",
    "CAPS_FORMAT",
    "PLAN_FORMAT",
    "BankFlow",
    "AccountBounds",
    "ReconciliationPlan",
    "parse_flows",
    "parse_caps",
    "account_bounds",
    "plan_reconciliation",
]

"""Semester fee billing for *confirmed* mutual-recognition allocations.

Fee model
---------
* Only **charged** records consume the semester point ladder.  A record is
  *exempt* (免缴) when the local course chosen in the confirmed plan appears in
  the policy's ``exempt_courses``.  Exempt records cost nothing and occupy no
  ladder position (免缴项不占档).
* Charged records are placed on a single per-student / per-semester ladder in
  registration order: ``sequence`` ascending, ties broken by the stable
  ``record_id``.  The key is total and deterministic, so material that arrives
  later but is *irrelevant* to billing (an exempt or unused record, or a record
  registered later) never reorders points already billed
  (无关材料迟到不改变已有先后).
* The ladder is marginal (telescoping): the first ``up_to`` points cost
  ``unit_price`` each, the next band the next price, and so on.  A record that
  straddles a band boundary is priced band by band
  (一条横跨价档要按档分别计费).

Every bill line records the exact ladder span and per-band segments the record
occupied (``start_position`` / ``end_position`` / ``segments``), which is what
lets a re-confirmed allocation be re-priced: when an earlier record becomes
exempt, a later record with unchanged points and account can shift into a
cheaper or dearer band and must be re-billed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from .errors import SchemaError
from .model import Records, content_hash

BILLING_POLICY_FORMAT = "semester-billing/1"
BILL_FORMAT = "semester-bill/1"

MAX_RECORDS = 12
MAX_ACCOUNTS = 6


def _require(cond: bool, message: str, *, location: Optional[str] = None) -> None:
    if not cond:
        raise SchemaError(message, location=location)


def _as_int(value: Any, *, name: str, location: str, minimum: Optional[int] = None) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{name} must be an integer",
        location=location,
    )
    if minimum is not None:
        _require(value >= minimum, f"{name} must be >= {minimum}", location=location)
    return value


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tier:
    """One marginal price band.

    ``up_to`` is the *cumulative* point position at which the band ends
    (inclusive); ``None`` means the final, unbounded band.
    """

    up_to: Optional[int]
    unit_price: int


@dataclass(frozen=True)
class BillingPolicy:
    semester: str
    tiers: Tuple[Tier, ...]
    exempt_courses: FrozenSet[str]
    record_accounts: Dict[str, Tuple[str, int]]  # record_id -> (account_id, sequence)
    raw: Dict[str, Any]

    def content_hash(self) -> str:
        return content_hash(self.raw)


def parse_billing_policy(doc: Any) -> BillingPolicy:
    _require(isinstance(doc, dict), "billing policy must be a JSON object")
    _require(
        doc.get("format") == BILLING_POLICY_FORMAT,
        f"billing policy format must be {BILLING_POLICY_FORMAT!r}",
        location="format",
    )
    semester = doc.get("semester")
    _require(
        isinstance(semester, str) and semester.strip() != "",
        "semester must be a non-empty string",
        location="semester",
    )

    raw_tiers = doc.get("tiers")
    _require(isinstance(raw_tiers, list) and raw_tiers, "tiers must be a non-empty list", location="tiers")
    tiers = []
    prev_up_to = 0
    for idx, entry in enumerate(raw_tiers):
        loc = f"tiers[{idx}]"
        _require(isinstance(entry, dict), "tier must be an object", location=loc)
        up_to = entry.get("up_to")
        is_last = idx == len(raw_tiers) - 1
        if up_to is not None:
            up_to = _as_int(up_to, name="up_to", location=loc, minimum=1)
            _require(up_to > prev_up_to, f"tier up_to must strictly increase", location=loc)
            prev_up_to = up_to
        else:
            _require(is_last, "only the final tier may be unbounded (up_to = null)", location=loc)
        unit_price = _as_int(entry.get("unit_price"), name="unit_price", location=loc, minimum=0)
        tiers.append(Tier(up_to=up_to, unit_price=unit_price))

    raw_exempt = doc.get("exempt_courses", [])
    _require(isinstance(raw_exempt, list), "exempt_courses must be a list", location="exempt_courses")
    exempt = set()
    for idx, cid in enumerate(raw_exempt):
        _require(
            isinstance(cid, str) and cid.strip() != "",
            "exempt course id must be a non-empty string",
            location=f"exempt_courses[{idx}]",
        )
        exempt.add(cid)

    raw_records = doc.get("record_accounts")
    _require(isinstance(raw_records, dict), "record_accounts must be an object", location="record_accounts")
    record_accounts: Dict[str, Tuple[str, int]] = {}
    for rid, entry in raw_records.items():
        loc = f"record_accounts[{rid}]"
        _require(isinstance(rid, str) and rid.strip() != "", "record id key must be non-empty", location=loc)
        _require(isinstance(entry, dict), "record_accounts entry must be an object", location=loc)
        account = entry.get("account_id")
        _require(
            isinstance(account, str) and account.strip() != "",
            "account_id must be a non-empty string",
            location=loc,
        )
        sequence = _as_int(entry.get("sequence"), name="sequence", location=loc, minimum=0)
        record_accounts[rid] = (account, sequence)
    _require(
        len({a for a, _ in record_accounts.values()}) <= MAX_ACCOUNTS,
        f"at most {MAX_ACCOUNTS} accounts are supported",
        location="record_accounts",
    )

    return BillingPolicy(
        semester=semester,
        tiers=tuple(tiers),
        exempt_courses=frozenset(exempt),
        record_accounts=record_accounts,
        raw=doc,
    )


# --------------------------------------------------------------------------- #
# bill lines
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecordCharge:
    record_id: str
    course_id: str
    account_id: str
    sequence: int
    points: int
    exempt: bool
    start_position: Optional[int]  # 1-based inclusive, None when exempt
    end_position: Optional[int]
    segments: Tuple[Dict[str, Any], ...]
    amount: int

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "course_id": self.course_id,
            "account_id": self.account_id,
            "sequence": self.sequence,
            "points": self.points,
            "exempt": self.exempt,
            "start_position": self.start_position,
            "end_position": self.end_position,
            "segments": [dict(s) for s in self.segments],
            "amount": self.amount,
        }


@dataclass(frozen=True)
class SemesterBill:
    semester: str
    student_id: str
    accounts: Dict[str, int]
    lines: Tuple[RecordCharge, ...]
    total_charged_points: int
    policy_hash: str

    def as_dict(self) -> dict:
        return {
            "semester": self.semester,
            "student_id": self.student_id,
            "accounts": {k: self.accounts[k] for k in sorted(self.accounts)},
            "lines": [line.as_dict() for line in self.lines],
            "total_charged_points": self.total_charged_points,
            "policy_hash": self.policy_hash,
        }


def _charge_span(
    tiers: Tuple[Tier, ...], offset: int, npoints: int
) -> Tuple[Tuple[Dict[str, Any], ...], int]:
    """Price ``npoints`` consecutive ladder points starting at ``offset``.

    ``offset`` is the number of points already placed on the ladder (0-based).
    Returns the per-band segments and the new offset.  Raises when the defined
    bands do not cover every point (no unbounded final band).
    """
    segments = []
    pos = offset
    remaining = npoints
    for idx, tier in enumerate(tiers):
        if remaining <= 0:
            break
        if tier.up_to is None:
            take = remaining
        else:
            take = min(remaining, tier.up_to - pos)
            if take < 0:
                take = 0
        if take > 0:
            segments.append(
                {
                    "tier_index": idx,
                    "unit_price": tier.unit_price,
                    "points": take,
                    "amount": take * tier.unit_price,
                    "from_position": pos + 1,
                    "to_position": pos + take,
                }
            )
            pos += take
            remaining -= take
    if remaining > 0:
        raise SchemaError(
            "price tiers do not cover all charged points; add a final unbounded tier",
            location="tiers",
        )
    return tuple(segments), pos


def price_semester(
    policy: BillingPolicy,
    records: Records,
    assignments: Mapping[str, str],
) -> SemesterBill:
    """Price the confirmed ``assignments`` (record_id -> chosen course_id).

    Points are always read from ``records``; the caller cannot inflate them.
    Only assigned records are billed.  Ordering and exemption follow
    :mod:`gradaudit.billing`'s model.
    """
    _require(
        len(assignments) <= MAX_RECORDS,
        f"at most {MAX_RECORDS} records are supported (got {len(assignments)})",
        location="assignments",
    )

    staged = []
    for rid, cid in assignments.items():
        rec = records.records.get(rid)
        if rec is None:
            raise SchemaError(
                f"record {rid!r} is not present in records revision {records.revision}",
                location=rid,
            )
        if rid not in policy.record_accounts:
            raise SchemaError(
                f"record {rid!r} has no account/sequence entry in the billing policy",
                location=rid,
            )
        account_id, sequence = policy.record_accounts[rid]
        exempt = cid in policy.exempt_courses
        staged.append((sequence, rid, cid, account_id, rec.points, exempt))

    # A stable total order: registration sequence, then stable record id.
    staged.sort(key=lambda item: (item[0], item[1]))

    position = 0
    accounts: Dict[str, int] = {}
    lines = []
    for sequence, rid, cid, account_id, points, exempt in staged:
        accounts.setdefault(account_id, 0)
        if exempt:
            line = RecordCharge(
                record_id=rid,
                course_id=cid,
                account_id=account_id,
                sequence=sequence,
                points=points,
                exempt=True,
                start_position=None,
                end_position=None,
                segments=(),
                amount=0,
            )
        else:
            segments, end_position = _charge_span(policy.tiers, position, points)
            amount = sum(seg["amount"] for seg in segments)
            line = RecordCharge(
                record_id=rid,
                course_id=cid,
                account_id=account_id,
                sequence=sequence,
                points=points,
                exempt=False,
                start_position=position + 1,
                end_position=end_position,
                segments=segments,
                amount=amount,
            )
            position = end_position
        accounts[account_id] += line.amount
        lines.append(line)

    return SemesterBill(
        semester=policy.semester,
        student_id=records.student_id,
        accounts=accounts,
        lines=tuple(lines),
        total_charged_points=position,
        policy_hash=policy.content_hash(),
    )


# --------------------------------------------------------------------------- #
# signed-bill helpers
# --------------------------------------------------------------------------- #
def bill_body(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """The immutable body a bill signature covers (everything but ``signature``)."""
    return {k: v for k, v in doc.items() if k != "signature"}


def sign_bill_document(body: Mapping[str, Any]) -> Dict[str, Any]:
    body = dict(body)
    return {**body, "signature": content_hash(body)}


def verify_bill_signature(doc: Mapping[str, Any]) -> bool:
    sig = doc.get("signature")
    if not isinstance(sig, str):
        return False
    return sig == content_hash(bill_body(doc))


def bill_account_delta(
    new_accounts: Mapping[str, int], previous_accounts: Optional[Mapping[str, int]]
) -> Dict[str, int]:
    """Per-account ``new - previous`` over the union of both account sets."""
    prev = previous_accounts or {}
    accounts = set(new_accounts) | set(prev)
    return {a: int(new_accounts.get(a, 0)) - int(prev.get(a, 0)) for a in sorted(accounts)}


__all__ = [
    "BILLING_POLICY_FORMAT",
    "BILL_FORMAT",
    "Tier",
    "BillingPolicy",
    "RecordCharge",
    "SemesterBill",
    "parse_billing_policy",
    "price_semester",
    "bill_body",
    "sign_bill_document",
    "verify_bill_signature",
    "bill_account_delta",
]

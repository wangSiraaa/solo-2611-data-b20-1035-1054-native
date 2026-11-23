"""Transferable-credit ledger (可转学时底单).

Built from an *already confirmed* allocation, never from a free-form wish list:

* Only **selected** records contribute.  An unused record can never open a line
  ("未用的记录不能开单").
* Each selected record's local course is looked up in the credit protocol to
  find its issuing school, credit kind and exact exchange ratio.
* All used records sharing the same ``(issuing_school, kind)`` are first timed
  individually as exact Fractions, then *pooled* and floored **once** to a
  whole, stampable number of credits.  The sub-credit remainder is retained on
  that same pool and is never moved to another kind ("余数仍归这一张底单、不可
  挪到另一个种类").
* The floor is per pool, never across pools, so no rounding surplus can be
  laundered from one credit kind into another.

``build_ledger`` is pure: pass the plan and the exact revisions it was confirmed
under.  ``Store.build_ledger`` supplies the confirmed plan plus its pinned
revisions so an off-campus allocation is processed under the revision that was
actually confirmed ("按其实际确认版本办理").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .errors import LedgerError
from .model import Catalog, Plan, Records
from .protocol import Protocol
from .rationals import floor_fraction, format_ratio

PoolKey = Tuple[str, str]  # (issuing_school, kind)


@dataclass(frozen=True)
class LineItem:
    """One selected transfer record and the exact credits it contributes."""

    record_id: str
    course_id: str
    issuing_school: str
    kind: str
    points: int
    ratio: Fraction
    exact_credits: Fraction

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "course_id": self.course_id,
            "issuing_school": self.issuing_school,
            "kind": self.kind,
            "points": self.points,
            "ratio": format_ratio(self.ratio),
            "exact_credits": format_ratio(self.exact_credits),
        }


@dataclass
class Pool:
    """One (issuing school, kind) credit pool on the ledger."""

    issuing_school: str
    kind: str
    items: List[LineItem] = field(default_factory=list)

    @property
    def total_exact(self) -> Fraction:
        total = Fraction(0)
        for it in self.items:
            total += it.exact_credits
        return total

    @property
    def stampable(self) -> int:
        """Whole credits that may be stamped (= floor of the exact pool total)."""
        return floor_fraction(self.total_exact)

    @property
    def remainder(self) -> Fraction:
        """Sub-credit remainder retained on *this* pool (never moved elsewhere)."""
        return self.total_exact - self.stampable

    @property
    def remainder_is_zero(self) -> bool:
        return self.remainder == 0

    @property
    def remainder_voucher(self) -> Optional[str]:
        """Receipt id for the retained sub-credit tail, or ``None`` if none.

        The remainder is not discarded: it is held on this pool under a
        deterministic voucher id so a later top-up can be accounted for without
        pretending the fractional tail was a whole credit.
        """
        if self.remainder_is_zero:
            return None
        return f"REMAINDER::{self.issuing_school}::{self.kind}"

    def as_dict(self) -> dict:
        return {
            "issuing_school": self.issuing_school,
            "kind": self.kind,
            "records": [it.record_id for it in self.items],
            "total_exact": format_ratio(self.total_exact),
            "stampable": self.stampable,
            "remainder": format_ratio(self.remainder),
            "remainder_voucher": self.remainder_voucher,
            "lines": [it.as_dict() for it in self.items],
        }


@dataclass
class Ledger:
    student_id: str
    catalog_revision: int
    records_revision: int
    request_id: str
    requirements_revision: int
    protocol_revision: int
    pools: Dict[PoolKey, Pool] = field(default_factory=dict)
    unused_records: List[str] = field(default_factory=list)

    def pool(self, issuing_school: str, kind: str) -> Pool:
        try:
            return self.pools[(issuing_school, kind)]
        except KeyError:
            raise LedgerError(
                f"ledger has no {kind!r} pool issued by {issuing_school!r}",
                location=f"{issuing_school}:{kind}",
            ) from None

    def total_stampable(self) -> int:
        return sum(p.stampable for p in self.pools.values())

    def as_dict(self) -> dict:
        return {
            "student_id": self.student_id,
            "request_id": self.request_id,
            "revisions": {
                "catalog_revision": self.catalog_revision,
                "records_revision": self.records_revision,
                "requirements_revision": self.requirements_revision,
                "protocol_revision": self.protocol_revision,
            },
            "pools": [self.pools[k].as_dict() for k in sorted(self.pools)],
            "unused_records": list(self.unused_records),
            "total_stampable": self.total_stampable(),
        }

    def appeal_lines(self) -> List[dict]:
        """Per-record appeal trail: actual course, destination pool, remainder.

        Each row answers the appeal questions one by one: where did the course
        actually go, which pool did it feed, and did its sub-credit tail get
        recombined into a voucher on that pool?
        """
        rows: List[dict] = []
        for key in sorted(self.pools):
            pool = self.pools[key]
            for it in sorted(pool.items, key=lambda x: x.record_id):
                rows.append({
                    "record_id": it.record_id,
                    "course_id": it.course_id,
                    "issuing_school": it.issuing_school,
                    "kind": it.kind,
                    "points": it.points,
                    "ratio": format_ratio(it.ratio),
                    "exact_credits": format_ratio(it.exact_credits),
                    "pool": {"issuing_school": it.issuing_school, "kind": it.kind},
                    "pool_stampable": pool.stampable,
                    "pool_remainder": format_ratio(pool.remainder),
                    "remainder_voucher": pool.remainder_voucher,
                })
        return rows


def build_ledger(
    plan: Plan,
    catalog: Catalog,
    records: Records,
    protocol: Protocol,
) -> Ledger:
    """Build a ledger from a plan using the exact revisions it references.

    The plan is expected to be a confirmed allocation.  This function does not
    re-run the requirement verifier (that is :func:`gradaudit.verify.verify_plan`);
    it enforces the ledger-specific rules instead:

    * every selected record exists in the pinned records revision;
    * no record is selected twice (no splitting);
    * the selected local course is a candidate for that record's external
      course under the pinned catalog revision;
    * the selected course has a protocol entry (issuing school / kind / ratio).
    """
    if records.revision != plan.records_revision:
        raise LedgerError(
            f"records revision {records.revision} != plan's confirmed {plan.records_revision}",
            location="records_revision",
        )
    if catalog.revision != plan.catalog_revision:
        raise LedgerError(
            f"catalog revision {catalog.revision} != plan's confirmed {plan.catalog_revision}",
            location="catalog_revision",
        )

    seen = set()
    pools: Dict[PoolKey, Pool] = {}
    used: List[str] = []

    for a in plan.assignments:
        if a.record_id in seen:
            raise LedgerError(
                f"record {a.record_id!r} selected more than once; points cannot be split",
                location=a.record_id,
            )
        seen.add(a.record_id)

        rec = records.records.get(a.record_id)
        if rec is None:
            raise LedgerError(
                f"record {a.record_id!r} is not present in confirmed records revision",
                location=a.record_id,
            )
        candidates = catalog.candidates(rec.external_course)
        if a.course_id not in candidates:
            raise LedgerError(
                f"course {a.course_id!r} is not a candidate for external course "
                f"{rec.external_course!r} under the confirmed catalog revision",
                location=a.record_id,
            )
        entry = protocol.entry(a.course_id)
        line = LineItem(
            record_id=a.record_id,
            course_id=a.course_id,
            issuing_school=entry.issuing_school,
            kind=entry.kind,
            points=rec.points,
            ratio=entry.ratio,
            exact_credits=entry.ratio * rec.points,
        )
        key = (entry.issuing_school, entry.kind)
        pool = pools.get(key)
        if pool is None:
            pool = Pool(issuing_school=entry.issuing_school, kind=entry.kind)
            pools[key] = pool
        pool.items.append(line)
        used.append(a.record_id)

    unused = sorted(set(records.records) - set(used))
    return Ledger(
        student_id=plan.student_id,
        catalog_revision=plan.catalog_revision,
        records_revision=plan.records_revision,
        request_id=plan.request_id,
        requirements_revision=plan.requirements_revision,
        protocol_revision=protocol.revision,
        pools=pools,
        unused_records=unused,
    )


__all__ = ["Ledger", "Pool", "LineItem", "PoolKey", "build_ledger"]

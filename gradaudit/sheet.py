"""Master sheet (底单): what the registry puts under the carbon press.

A sheet is built from the *live* gradaudit confirmation of one audit request
-- the inter-school office reuses gradaudit's formal credit confirmation, and
a confirmed allocation submitted by an external system takes the same path
(``Store.prepare_audit`` + ``confirm_audit`` accept any plan that passes the
independent verifier).  Only records actually used by the confirmed plan
become rows; unused records never enter the sheet.  Each row keeps

* ``record_id``  -- the stable transfer-record id,
* ``course_id``  -- the local course chosen on the confirmation,
* ``group_id``   -- the group the record was counted toward,
* ``points``     -- read from the *pinned* records revision of that
  confirmation, never from the plan and never from a later revision.

A sheet whose confirmation has gone stale (catalog / records / requirements
moved on) cannot be printed: the registry must re-confirm first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .errors import SheetError
from .library import Store
from .model import content_hash, parse_plan

MAX_ROWS = 8

SHEET_FORMAT = "master-sheet/1"


@dataclass(frozen=True)
class SheetRow:
    record_id: str
    course_id: str
    group_id: str
    points: int

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "course_id": self.course_id,
            "group_id": self.group_id,
            "points": self.points,
        }


@dataclass(frozen=True)
class MasterSheet:
    sheet_id: str
    student_id: str
    request_id: str
    confirmation_id: str
    catalog_revision: int
    records_revision: int
    requirements_revision: int
    rows: Tuple[SheetRow, ...]

    def as_document(self) -> dict:
        return {
            "format": SHEET_FORMAT,
            "sheet_id": self.sheet_id,
            "student_id": self.student_id,
            "request_id": self.request_id,
            "confirmation_id": self.confirmation_id,
            "catalog_revision": self.catalog_revision,
            "records_revision": self.records_revision,
            "requirements_revision": self.requirements_revision,
            "rows": [r.as_dict() for r in self.rows],
        }

    def content_hash(self) -> str:
        return content_hash(self.as_document())


def build_master_sheet(store: Store, request_id: str) -> MasterSheet:
    """Build the master sheet for a request's live confirmation.

    Raises :class:`SheetError` when there is no live confirmation or the
    confirmation has gone stale (revisions moved since it was prepared).
    """
    status = store.confirmation_status(request_id)
    if not status.get("confirmed"):
        raise SheetError(
            f"request {request_id!r} has no current confirmation to print from "
            f"(status: {status.get('reason') or 'stale'})",
            location=request_id,
            detail=status.get("stale"),
        )
    confirmation_id = status["confirmation_id"]
    revisions = status["revisions"]

    plan_doc = None
    for conf in store.list_confirmations(request_id):
        if conf["confirmation_id"] == confirmation_id:
            plan_doc = conf["plan"]
            break
    if plan_doc is None:  # pragma: no cover - defensive
        raise SheetError(
            f"confirmation {confirmation_id!r} not found in history",
            location=confirmation_id,
        )
    plan = parse_plan(plan_doc)

    records = store.get_records(revisions["records_revision"])
    if records is None:  # pragma: no cover - defensive
        raise SheetError(
            f"records revision {revisions['records_revision']} is no longer available",
            location="records_revision",
        )

    rows: List[SheetRow] = []
    for a in sorted(plan.assignments, key=lambda x: x.record_id):
        rec = records.records.get(a.record_id)
        if rec is None:  # pragma: no cover - the verifier accepted this plan
            raise SheetError(
                f"record {a.record_id!r} is missing from records revision "
                f"{records.revision}",
                location=a.record_id,
            )
        rows.append(SheetRow(
            record_id=a.record_id,
            course_id=a.course_id,
            group_id=a.group_id,
            points=rec.points,  # points always from the pinned records revision
        ))

    if not rows:
        raise SheetError(
            f"confirmation for {request_id!r} uses no records; nothing to print",
            location=request_id,
        )
    if len(rows) > MAX_ROWS:
        raise SheetError(
            f"a master sheet holds at most {MAX_ROWS} credit rows (got {len(rows)})",
            location=request_id,
        )

    sheet_id = "SH-" + content_hash({
        "confirmation_id": confirmation_id,
        "rows": [r.as_dict() for r in rows],
    })[:16]
    return MasterSheet(
        sheet_id=sheet_id,
        student_id=plan.student_id,
        request_id=request_id,
        confirmation_id=confirmation_id,
        catalog_revision=revisions["catalog_revision"],
        records_revision=revisions["records_revision"],
        requirements_revision=revisions["requirements_revision"],
        rows=tuple(rows),
    )


__all__ = ["SheetRow", "MasterSheet", "build_master_sheet", "MAX_ROWS", "SHEET_FORMAT"]

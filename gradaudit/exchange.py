"""Cross-school articulation compatibility testing (the "teaching lab").

A *transcript* is a small multiset of transfer records.  Each record carries a
source school's external course **code** and the **full positive-integer points**;
nothing else crosses the interface.  The receiving school freely re-allocates
the same transcript with its *own* catalog and the *same* graduation
requirements (candidate local course -> category -> group).

The consortium fixes an *exchange scope*: which source codes are admitted, which
point values each may carry, the maximum number of records per transcript, and
how many graduation-requirement documents are in force.

A directed channel ``sender -> receiver`` at a fixed pair of catalog versions is
**compatible** -- a signed ``compatibility-form/1`` -- iff *every* in-scope
transcript that the sender can satisfy under the shared requirements the
receiver can also satisfy::

    forall T in scope:  feasible(sender_catalog, T, R)  ==>  feasible(receiver_catalog, T, R)

Because the scope is finite and tiny (<=3 external codes, <=2 point values,
<=4 records) we certify this universally by **exhaustive enumeration**, never by
comparing same-named courses and never by checking each group's total in
isolation.  A failing form carries a real *witness* transcript:

* a sender allocation that the sender's own library independently accepts
  (:func:`gradaudit.verify.verify_plan`), and
* an exact proof that the receiver is **globally** infeasible
  (:func:`gradaudit.solver.solve`) -- even though the receiver may pick a
  different allocation and even though each group might be reachable alone.

The exact solver proves infeasibility; a witness therefore cannot be explained
away by "each group had enough points".
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import SchemaError
from .model import (
    Catalog,
    Records,
    Requirements,
    canonical_json,
    content_hash,
    parse_records,
)
from .solver import solve
from .verify import verify_plan

# --------------------------------------------------------------------------- #
# contract caps (small consortium / window)
# --------------------------------------------------------------------------- #
MAX_EXTERNAL_CODES = 3
MAX_POINT_VALUES = 2
MAX_TRANSCRIPT_RECORDS = 4
MAX_REQUIREMENTS = 2

SCOPE_FORMAT = "exchange-scope/1"
FORM_FORMAT = "compatibility-form/1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# exchange scope
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scope:
    """The consortium's finite exchange scope."""

    external_courses: Dict[str, Tuple[int, ...]]
    max_records: int
    max_requirements: int

    def tokens(self) -> Tuple[Tuple[str, int], ...]:
        """All (code, points) pairs admitted by the scope, sorted canonically."""
        out: List[Tuple[str, int]] = []
        for code in sorted(self.external_courses):
            for pts in sorted(set(self.external_courses[code])):
                out.append((code, pts))
        return tuple(out)

    def raw_document(self) -> dict:
        return {
            "format": SCOPE_FORMAT,
            "external_courses": {
                code: sorted(set(self.external_courses[code]))
                for code in sorted(self.external_courses)
            },
            "max_records": self.max_records,
            "max_requirements": self.max_requirements,
        }

    def content_hash(self) -> str:
        return content_hash(self.raw_document())


def parse_scope(doc: object) -> Scope:
    if not isinstance(doc, dict):
        raise SchemaError("scope must be a JSON object")
    if doc.get("format") != SCOPE_FORMAT:
        raise SchemaError(f"scope format must be {SCOPE_FORMAT!r}", location="format")

    raw = doc.get("external_courses")
    if not isinstance(raw, dict) or not raw:
        raise SchemaError("external_courses must be a non-empty object", location="external_courses")
    if len(raw) > MAX_EXTERNAL_CODES:
        raise SchemaError(
            f"at most {MAX_EXTERNAL_CODES} external codes are supported (got {len(raw)})",
            location="external_courses",
        )
    courses: Dict[str, Tuple[int, ...]] = {}
    distinct_points: set = set()
    for code, points in raw.items():
        if not isinstance(code, str) or code.strip() == "":
            raise SchemaError("external code must be a non-empty string", location="external_courses")
        if not isinstance(points, list) or not points:
            raise SchemaError(f"points for {code!r} must be a non-empty list", location=code)
        norm: List[int] = []
        for p in points:
            if not isinstance(p, int) or isinstance(p, bool) or p < 1:
                raise SchemaError(
                    f"points for {code!r} must be positive integers", location=code
                )
            if p > 6:
                raise SchemaError(
                    f"points for {code!r} exceed the library scale 1..6", location=code
                )
            if p not in norm:
                norm.append(p)
        distinct_points.update(norm)
        courses[code] = tuple(sorted(norm))

    if len(distinct_points) > MAX_POINT_VALUES:
        raise SchemaError(
            f"at most {MAX_POINT_VALUES} distinct point values are supported "
            f"(got {sorted(distinct_points)})",
            location="external_courses",
        )

    max_records = doc.get("max_records")
    if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
        raise SchemaError("max_records must be a positive integer", location="max_records")
    if max_records > MAX_TRANSCRIPT_RECORDS:
        raise SchemaError(
            f"at most {MAX_TRANSCRIPT_RECORDS} records per transcript are supported "
            f"(got {max_records})",
            location="max_records",
        )

    max_requirements = doc.get("max_requirements")
    if (
        not isinstance(max_requirements, int)
        or isinstance(max_requirements, bool)
        or max_requirements < 1
    ):
        raise SchemaError("max_requirements must be a positive integer", location="max_requirements")
    if max_requirements > MAX_REQUIREMENTS:
        raise SchemaError(
            f"at most {MAX_REQUIREMENTS} requirements are supported (got {max_requirements})",
            location="max_requirements",
        )

    return Scope(
        external_courses=courses,
        max_records=max_records,
        max_requirements=max_requirements,
    )


# --------------------------------------------------------------------------- #
# transcripts
# --------------------------------------------------------------------------- #
Transcript = Tuple[Tuple[str, int], ...]

DEFAULT_STUDENT_ID = "XCHG_STUDENT"


def enumerate_transcripts(scope: Scope) -> List[Transcript]:
    """Every in-scope transcript: a multiset of <=max_records (code, points) tokens.

    Codes may repeat (the same source course can appear on several records), but
    multiplicities of a single *token* are the only duplication possible, so a
    combinations-with-replacement enumeration covers the scope exactly.
    """
    tokens = scope.tokens()
    out: List[Transcript] = []
    for k in range(0, scope.max_records + 1):
        for combo in itertools.combinations_with_replacement(range(len(tokens)), k):
            out.append(tuple(tokens[i] for i in combo))
    return out


def records_for_transcript(
    transcript: Transcript,
    *,
    student_id: str = DEFAULT_STUDENT_ID,
    revision: int = 1,
) -> Records:
    """Build a real ``records`` document from a transcript (stable record ids)."""
    ordered = sorted(transcript)
    return parse_records(
        {
            "student_id": student_id,
            "revision": revision,
            "records": [
                {
                    "record_id": f"R{i + 1}",
                    "external_course": code,
                    "points": pts,
                }
                for i, (code, pts) in enumerate(ordered)
            ],
        }
    )


# --------------------------------------------------------------------------- #
# compatibility testing
# --------------------------------------------------------------------------- #
@dataclass
class RequirementOutcome:
    request_id: str
    requirements_revision: int
    compatible: bool
    witness: Optional[dict]
    transcripts_examined: int

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "requirements_revision": self.requirements_revision,
            "compatible": self.compatible,
            "witness": self.witness,
            "transcripts_examined": self.transcripts_examined,
        }


@dataclass
class CompatibilityResult:
    compatible: bool
    transcripts_in_scope: int
    per_requirement: List[RequirementOutcome] = field(default_factory=list)
    witness: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "compatible": self.compatible,
            "transcripts_in_scope": self.transcripts_in_scope,
            "per_requirement": [o.as_dict() for o in self.per_requirement],
            "witness": self.witness,
        }


def _witness_payload(
    transcript: Transcript,
    records: Records,
    requirement: Requirements,
    sender_result,
    receiver_result,
    sender_catalog: Catalog,
    receiver_catalog: Catalog,
) -> dict:
    sender_verify = verify_plan(
        sender_result.as_plan(), sender_catalog, records, requirement
    )
    if not sender_verify.accepted:
        # The exact solver must never emit a plan the independent verifier
        # rejects; if this ever fires the library itself is inconsistent.
        raise AssertionError(
            "internal error: sender solver plan rejected by the independent verifier: "
            f"{sender_verify.reasons}"
        )
    # The receiver must be *globally* infeasible -- not merely "no allocation was
    # found".  Re-confirm with the exact solver and record it in the witness.
    receiver_recheck = solve(receiver_catalog, records, requirement)
    if receiver_recheck.feasible:
        raise AssertionError(
            "internal error: receiver reported infeasible but the exact solver found "
            "an allocation"
        )

    maxima = [
        {
            "group_id": g.group_id,
            "minimum_points": g.minimum_points,
            "max_ignoring_other_groups": g.max_ignoring_other_groups,
            "individually_reachable": g.satisfied,
        }
        for g in receiver_result.groups
    ]
    return {
        "type": "transcript_witness",
        "requirement": {
            "request_id": requirement.request_id,
            "revision": requirement.revision,
        },
        "records": [
            {
                "record_id": r["record_id"],
                "external_course": r["external_course"],
                "points": r["points"],
            }
            for r in records.raw_document()["records"]
        ],
        "sender": {
            "feasible": True,
            "assignments": [
                {"record_id": a.record_id, "course_id": a.course_id, "group_id": a.group_id}
                for a in sender_result.assignments
            ],
            "group_totals": dict(sender_result.group_totals),
            "catalog_revision": sender_catalog.revision,
            "verified_by_original_library": True,
        },
        "receiver": {
            "feasible": False,
            "catalog_revision": receiver_catalog.revision,
            "group_max_ignoring_other_groups": maxima,
            "note": receiver_result.note,
        },
        "explanation": (
            "The sender has a legal allocation for this transcript (independently "
            "verified), but the receiver is globally infeasible even though it may "
            "choose any allocation and even if some group is reachable alone."
        ),
    }


def _check_requirement(
    sender_catalog: Catalog,
    receiver_catalog: Catalog,
    requirement: Requirements,
    transcripts: Sequence[Transcript],
) -> RequirementOutcome:
    examined = 0
    for transcript in transcripts:
        examined += 1
        records = records_for_transcript(transcript)
        sender_result = solve(sender_catalog, records, requirement)
        if not sender_result.feasible:
            continue  # sender does not accept this transcript; out of the relation
        receiver_result = solve(receiver_catalog, records, requirement)
        if receiver_result.feasible:
            continue  # receiver can also catch it
        witness = _witness_payload(
            transcript,
            records,
            requirement,
            sender_result,
            receiver_result,
            sender_catalog,
            receiver_catalog,
        )
        return RequirementOutcome(
            request_id=requirement.request_id,
            requirements_revision=requirement.revision,
            compatible=False,
            witness=witness,
            transcripts_examined=examined,
        )
    return RequirementOutcome(
        request_id=requirement.request_id,
        requirements_revision=requirement.revision,
        compatible=True,
        witness=None,
        transcripts_examined=examined,
    )


def check_compatibility(
    sender_catalog: Catalog,
    receiver_catalog: Catalog,
    requirements_list: Sequence[Requirements],
    scope: Scope,
) -> CompatibilityResult:
    """Universally certify (or refute) a directed channel at fixed versions.

    Returns ``compatible=True`` only if every requirement holds for every
    in-scope transcript.  On the first failure it returns a concrete witness.
    """
    if len(requirements_list) > scope.max_requirements:
        raise SchemaError(
            f"at most {scope.max_requirements} requirements are in scope "
            f"(got {len(requirements_list)})",
            location="requirements",
        )
    transcripts = enumerate_transcripts(scope)
    per_req: List[RequirementOutcome] = []
    overall = True
    first_witness: Optional[dict] = None
    for req in requirements_list:
        outcome = _check_requirement(sender_catalog, receiver_catalog, req, transcripts)
        per_req.append(outcome)
        if not outcome.compatible:
            overall = False
            if first_witness is None:
                first_witness = outcome.witness
    return CompatibilityResult(
        compatible=overall,
        transcripts_in_scope=len(transcripts),
        per_requirement=per_req,
        witness=first_witness,
    )


def audit_scope(
    sender_catalog: Catalog,
    receiver_catalog: Catalog,
    requirements_list: Sequence[Requirements],
    scope: Scope,
) -> List[dict]:
    """Per-transcript verdicts for every requirement in scope.

    This is the lab's raw worksheet: for each requirement and each in-scope
    transcript it records whether the sender accepts and whether the receiver
    accepts.  A transcript the sender accepts but the receiver does not is a
    violation.  The aggregate :func:`check_compatibility` is what becomes a
    signed form; this detail is for independent external review.
    """
    transcripts = enumerate_transcripts(scope)
    rows: List[dict] = []
    for req in requirements_list:
        for transcript in transcripts:
            records = records_for_transcript(transcript)
            s = solve(sender_catalog, records, req)
            r = solve(receiver_catalog, records, req)
            rows.append({
                "request_id": req.request_id,
                "requirements_revision": req.revision,
                "transcript": [
                    {"external_course": code, "points": pts} for code, pts in transcript
                ],
                "sender_accepts": s.feasible,
                "receiver_accepts": r.feasible,
                "violation": s.feasible and not r.feasible,
            })
    return rows


def sign_channel_forms(
    *,
    sender_school: str,
    receiver_school: str,
    sender_old: Catalog,
    sender_new: Catalog,
    receiver_old: Catalog,
    receiver_new: Catalog,
    requirements_list: Sequence[Requirements],
    scope: Scope,
) -> List[CompatibilityForm]:
    """Sign the four version combos of one directed channel."""
    forms: List[CompatibilityForm] = []
    for sv, sc in (("old", sender_old), ("new", sender_new)):
        for rv, rc in (("old", receiver_old), ("new", receiver_new)):
            forms.append(sign_form(
                sc, rc, requirements_list, scope,
                sender_school=sender_school, receiver_school=receiver_school,
                sender_version=sv, receiver_version=rv,
            ))
    return forms


def verify_witness(
    witness: dict,
    sender_catalog: Catalog,
    receiver_catalog: Catalog,
    requirement: Requirements,
) -> dict:
    """Re-check a witness from scratch using the original library.

    * rebuilds the transcript's records,
    * verifies the sender allocation independently (``verify_plan``),
    * re-solves the receiver exactly and confirms global infeasibility.
    """
    raw_records = witness.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise SchemaError("witness has no records", location="witness")
    records = parse_records(
        {
            "student_id": DEFAULT_STUDENT_ID,
            "revision": 1,
            "records": raw_records,
        }
    )
    sender_plan = {
        "format": "course-allocation/1",
        "student_id": DEFAULT_STUDENT_ID,
        "catalog_revision": sender_catalog.revision,
        "records_revision": 1,
        "request_id": requirement.request_id,
        "requirements_revision": requirement.revision,
        "assignments": witness["sender"]["assignments"],
    }
    from .verify import verify_plan_document

    sender_check = verify_plan_document(sender_plan, sender_catalog, records, requirement)
    receiver_result = solve(receiver_catalog, records, requirement)
    return {
        "sender_accepted": sender_check.accepted,
        "sender_reasons": sender_check.reasons,
        "receiver_feasible": receiver_result.feasible,
        "receiver_group_maxima": [
            {
                "group_id": g.group_id,
                "minimum_points": g.minimum_points,
                "max_ignoring_other_groups": g.max_ignoring_other_groups,
            }
            for g in receiver_result.groups
        ],
        "witness_valid": sender_check.accepted and not receiver_result.feasible,
    }


# --------------------------------------------------------------------------- #
# signed compatibility forms
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CompatibilityForm:
    sender_school: str
    receiver_school: str
    sender_version: str
    receiver_version: str
    sender_catalog_hash: str
    receiver_catalog_hash: str
    scope_hash: str
    requirements_hash: str
    result: dict
    signed_at: str

    @property
    def compatible(self) -> bool:
        return bool(self.result.get("compatible"))

    def as_document(self) -> dict:
        return {
            "format": FORM_FORMAT,
            "channel": {
                "sender_school": self.sender_school,
                "receiver_school": self.receiver_school,
            },
            "versions": {
                "sender": self.sender_version,
                "receiver": self.receiver_version,
            },
            "sender_catalog_hash": self.sender_catalog_hash,
            "receiver_catalog_hash": self.receiver_catalog_hash,
            "scope_hash": self.scope_hash,
            "requirements_hash": self.requirements_hash,
            "compatible": self.compatible,
            "result": self.result,
            "signed_at": self.signed_at,
        }

    def _substance(self) -> dict:
        doc = self.as_document()
        doc.pop("signed_at", None)
        return doc

    def content_hash(self) -> str:
        return content_hash(self._substance())


def requirements_content_hash(requirements_list: Sequence[Requirements]) -> str:
    return content_hash([r.raw_document() for r in requirements_list])


def sign_form(
    sender_catalog: Catalog,
    receiver_catalog: Catalog,
    requirements_list: Sequence[Requirements],
    scope: Scope,
    *,
    sender_school: str,
    receiver_school: str,
    sender_version: str,
    receiver_version: str,
    signed_at: Optional[str] = None,
) -> CompatibilityForm:
    for version in (sender_version, receiver_version):
        if version not in ("old", "new"):
            raise SchemaError(
                f"version must be 'old' or 'new' (got {version!r})", location="version"
            )
    result = check_compatibility(
        sender_catalog, receiver_catalog, requirements_list, scope
    )
    return CompatibilityForm(
        sender_school=sender_school,
        receiver_school=receiver_school,
        sender_version=sender_version,
        receiver_version=receiver_version,
        sender_catalog_hash=sender_catalog.content_hash(),
        receiver_catalog_hash=receiver_catalog.content_hash(),
        scope_hash=scope.content_hash(),
        requirements_hash=requirements_content_hash(requirements_list),
        result=result.as_dict(),
        signed_at=signed_at or _now(),
    )


class CompatibilityStore:
    """SQLite-backed archive of signed compatibility forms.

    Forms are content-addressed by :meth:`CompatibilityForm.content_hash`; the
    signature is therefore reviewable *before* the switch window and cannot be
    silently swapped (recomputing the hash detects tampering).
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    @contextmanager
    def _txn(self):
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CompatibilityStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS compatibility_forms (
                form_hash TEXT PRIMARY KEY,
                sender_school TEXT NOT NULL,
                receiver_school TEXT NOT NULL,
                sender_version TEXT NOT NULL,
                receiver_version TEXT NOT NULL,
                sender_catalog_hash TEXT NOT NULL,
                receiver_catalog_hash TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                requirements_hash TEXT NOT NULL,
                compatible INTEGER NOT NULL,
                document_json TEXT NOT NULL,
                signed_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def save(self, form: CompatibilityForm) -> str:
        h = form.content_hash()
        with self._txn():
            self.conn.execute(
                """INSERT OR IGNORE INTO compatibility_forms(
                       form_hash, sender_school, receiver_school, sender_version,
                       receiver_version, sender_catalog_hash, receiver_catalog_hash,
                       scope_hash, requirements_hash, compatible, document_json, signed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    h,
                    form.sender_school,
                    form.receiver_school,
                    form.sender_version,
                    form.receiver_version,
                    form.sender_catalog_hash,
                    form.receiver_catalog_hash,
                    form.scope_hash,
                    form.requirements_hash,
                    1 if form.compatible else 0,
                    canonical_json(form.as_document()),
                    form.signed_at,
                ),
            )
        return h

    def get_form(self, form_hash: str) -> Optional[CompatibilityForm]:
        row = self.conn.execute(
            "SELECT document_json FROM compatibility_forms WHERE form_hash = ?",
            (form_hash,),
        ).fetchone()
        if row is None:
            return None
        return _form_from_document(json.loads(row["document_json"]))

    def list_forms(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT form_hash, sender_school, receiver_school, sender_version, "
            "receiver_version, compatible, signed_at FROM compatibility_forms "
            "ORDER BY signed_at, form_hash"
        ).fetchall()
        return [dict(r) for r in rows]

    def find_forms(
        self,
        *,
        sender_school: Optional[str] = None,
        receiver_school: Optional[str] = None,
        sender_version: Optional[str] = None,
        receiver_version: Optional[str] = None,
    ) -> List[CompatibilityForm]:
        clauses = []
        args: List[object] = []
        for col, val in (
            ("sender_school", sender_school),
            ("receiver_school", receiver_school),
            ("sender_version", sender_version),
            ("receiver_version", receiver_version),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT document_json FROM compatibility_forms"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY signed_at, form_hash"
        rows = self.conn.execute(sql, args).fetchall()
        return [_form_from_document(json.loads(r["document_json"])) for r in rows]


def _form_from_document(doc: dict) -> CompatibilityForm:
    if doc.get("format") != FORM_FORMAT:
        raise SchemaError(f"form format must be {FORM_FORMAT!r}", location="format")
    channel = doc["channel"]
    versions = doc["versions"]
    return CompatibilityForm(
        sender_school=channel["sender_school"],
        receiver_school=channel["receiver_school"],
        sender_version=versions["sender"],
        receiver_version=versions["receiver"],
        sender_catalog_hash=doc["sender_catalog_hash"],
        receiver_catalog_hash=doc["receiver_catalog_hash"],
        scope_hash=doc["scope_hash"],
        requirements_hash=doc["requirements_hash"],
        result=doc["result"],
        signed_at=doc.get("signed_at", ""),
    )


__all__ = [
    "Scope",
    "parse_scope",
    "Transcript",
    "enumerate_transcripts",
    "records_for_transcript",
    "CompatibilityResult",
    "RequirementOutcome",
    "check_compatibility",
    "audit_scope",
    "verify_witness",
    "CompatibilityForm",
    "sign_form",
    "sign_channel_forms",
    "requirements_content_hash",
    "CompatibilityStore",
    "SCOPE_FORMAT",
    "FORM_FORMAT",
]

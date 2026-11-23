"""Registry/counter handoff (注册处 ↔ 出证柜): sealed packages and press sessions.

The registry and the counter are separate posts; a relief clerk at the
counter never sees the underlying grades.  Two ways to hand work over:

* **Sealed package** (primary).  The registry picks the naming and the whole
  stack order for the paper numbers registered for this issuance, and signs a
  sealed press instruction.  The counter clerk verifies the physical slips
  against the package and presses.  If a slip turns out damaged *before*
  pressing, swapping in a spare voids the package: the registry must plan
  again and seal a new one.
* **Term permit**.  The registry issues a permit limited to the courses
  actually on the sheet, carrying the approved pairs *plus* the cross-row
  constraints (one course -- one pair everywhere; no two courses printed as
  the same complete pair).  The counter then plans the stack itself against
  the slips physically at hand.  A permit that only listed per-row allowed
  pairs would be insufficient: it would miss exactly those two constraints.

One sheet is pressed **once**.  After a press, if distribution stops midway
(top slip already torn away, receipts not yet registered), a relief clerk
resumes from the serials and action receipts the press retained locally --
continuing to distribute the remaining slips.  Pressing a second set for the
same sheet is refused.  The authorized registry can review afterwards which
colour channels each slip blocked during the press.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .errors import CounterStateError, PackageError, PressError
from .library import Store
from .model import canonical_json, content_hash
from .press import (
    POINTS_CHANNEL,
    FormSpec,
    PressField,
    PressPlan,
    RecipientSpec,
    plan_press,
    simulate_press,
)
from .sheet import MasterSheet, build_master_sheet
from .terms import TermOffice, TermPair

PACKAGE_FORMAT = "press-package/1"
PERMIT_FORMAT = "term-permit/1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sign(key: bytes, body: dict) -> str:
    return hmac.new(key, canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# registry office (注册处)
# --------------------------------------------------------------------------- #
class RegistryOffice:
    """The registry: builds sheets, seals packages, issues permits, reviews."""

    def __init__(self, store: Store, office: TermOffice, *, key: bytes):
        self.store = store
        self.office = office
        self.key = key

    # -- sheet --------------------------------------------------------------
    def build_sheet(self, request_id: str) -> MasterSheet:
        return build_master_sheet(self.store, request_id)

    # -- sealed packages ------------------------------------------------------
    def seal_package(
        self,
        sheet: MasterSheet,
        recipients: Sequence[RecipientSpec],
        stock: Sequence[FormSpec],
        *,
        exclude_serials: Sequence[str] = (),
    ) -> dict:
        """Plan a press and seal it as a signed instruction package.

        Raises :class:`PressError` carrying the located failure reasons when
        the job cannot be done (the registry never seals an impossible
        instruction).
        """
        result = plan_press(
            self.office, sheet, recipients, stock, exclude_serials=exclude_serials
        )
        if not isinstance(result, PressPlan):
            raise PressError(
                "cannot seal a press package: the job is infeasible",
                location=sheet.sheet_id,
                detail=result.as_dict(),
            )
        pair_registry = {
            cid: p.as_dict() for cid, p in sorted(result.naming.items())
        }
        body = {
            "format": PACKAGE_FORMAT,
            "sheet_id": sheet.sheet_id,
            "sheet_hash": sheet.content_hash(),
            "student_id": sheet.student_id,
            "request_id": sheet.request_id,
            "rows": [r.as_dict() for r in sheet.rows],
            "naming": {cid: p.pair_id for cid, p in sorted(result.naming.items())},
            "pair_registry": pair_registry,
            "stack": result.serials,
            "expected_forms": {f.serial: f.as_dict() for f in result.stack},
            "expected_imprints": {
                f.serial: result.imprints_document(f.serial) for f in result.stack
            },
        }
        signature = _sign(self.key, body)
        body["package_id"] = "PKG-" + signature[:16]
        body["signature"] = signature
        return body

    def reseal_after_damage(
        self,
        voided_package: dict,
        sheet: MasterSheet,
        recipients: Sequence[RecipientSpec],
        stock: Sequence[FormSpec],
        *,
        damaged_serial: str,
    ) -> dict:
        """Seal a replacement package after a slip was damaged before pressing.

        The old package must already be voided at the counter; the damaged
        serial is excluded from the new plan.
        """
        if voided_package.get("sheet_id") != sheet.sheet_id:
            raise PackageError(
                "voided package does not belong to this sheet",
                location=voided_package.get("package_id"),
            )
        return self.seal_package(
            sheet, recipients, stock, exclude_serials=[damaged_serial]
        )

    # -- term permit (alternative handover) ------------------------------------
    def issue_permit(self, sheet: MasterSheet) -> dict:
        """Issue a term permit limited to the courses actually on the sheet."""
        courses: Dict[str, dict] = {}
        for row in sheet.rows:
            entry = courses.setdefault(row.course_id, {"groups": set(), "approved_pairs": []})
            entry["groups"].add(row.group_id)
        for cid, entry in courses.items():
            pairs = []
            for pair in self.office.pairs_for_course(cid):
                pairs.append({
                    **pair.as_dict(),
                    "approved_groups": sorted(self.office.approved_groups(pair.pair_id)),
                })
            entry["approved_pairs"] = pairs
            entry["groups"] = sorted(entry["groups"])
        body = {
            "format": PERMIT_FORMAT,
            "sheet_id": sheet.sheet_id,
            "sheet_hash": sheet.content_hash(),
            "courses": courses,
            "constraints": [
                "pair_must_be_approved_for_every_actual_group",
                "same_course_same_pair_across_rows",
                "no_cross_course_identical_pair",
            ],
        }
        signature = _sign(self.key, body)
        body["permit_id"] = "TP-" + signature[:16]
        body["signature"] = signature
        return body

    # -- review ------------------------------------------------------------------
    def review_press(self, counter: "Counter", session_id: str) -> dict:
        """Authorized review: which colour channels each slip blocked."""
        status = counter.session_status(session_id)
        return {
            "session_id": session_id,
            "sheet_id": status["sheet_id"],
            "stack": [f["serial"] for f in status["forms"]],
            "blocked_channels": status["blocked_channels"],
            "actions": [a["kind"] for a in status["actions"]],
        }


# --------------------------------------------------------------------------- #
# counter (出证柜): local simulated press with a paper record
# --------------------------------------------------------------------------- #
class Counter:
    """The simulated counter.

    Holds no grades: only sealed packages, the physical slip cabinet, and its
    own paper record of sessions and action receipts.  A relief clerk opening
    a new ``Counter`` on the same paper-record file can continue an
    interrupted session without any access to the registry's grade store.
    """

    def __init__(self, db_path: str, *, key: bytes, stock: Sequence[FormSpec]):
        self.db_path = db_path
        self.key = key
        self.cabinet: Dict[str, FormSpec] = {f.serial: f for f in stock}
        self.damaged: set = set()
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS packages (
                signature TEXT PRIMARY KEY,
                package_json TEXT NOT NULL,
                status TEXT NOT NULL,           -- RECEIVED | CONSUMED | VOID
                received_at TEXT NOT NULL,
                voided_at TEXT,
                void_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                sheet_id TEXT NOT NULL,
                student_id TEXT NOT NULL,
                package_signature TEXT NOT NULL,
                status TEXT NOT NULL,           -- LOADED | PRESSED | STOPPED | COMPLETED | VOID
                created_at TEXT NOT NULL,
                pressed_at TEXT,
                stopped_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS session_forms (
                session_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                serial TEXT NOT NULL,
                recipient TEXT NOT NULL,
                imprints_json TEXT,
                blocked_json TEXT,
                distributed_at TEXT,
                receipt_registered_at TEXT,
                PRIMARY KEY (session_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Counter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- helpers ---------------------------------------------------------------
    def _action(self, session_id: str, kind: str, detail: dict) -> dict:
        cur = self.conn.execute(
            "INSERT INTO actions(session_id, kind, detail_json, at) VALUES (?,?,?,?)",
            (session_id, kind, json.dumps(detail, ensure_ascii=False, sort_keys=True), _now()),
        )
        return {"action_id": cur.lastrowid, "session_id": session_id, "kind": kind,
                "detail": detail}

    def _session(self, session_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise CounterStateError(f"unknown session {session_id!r}", location=session_id)
        return row

    def _package(self, signature: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT package_json, status FROM packages WHERE signature = ?", (signature,)
        ).fetchone()
        if row is None:
            return None
        return {"package": json.loads(row["package_json"]), "status": row["status"]}

    # -- intake ------------------------------------------------------------------
    def receive_package(self, package: dict) -> str:
        """Accept a sealed package; returns the new session id."""
        if package.get("format") != PACKAGE_FORMAT:
            raise PackageError("not a press package", location="format")
        signature = package.get("signature")
        body = {k: v for k, v in package.items() if k not in ("signature", "package_id")}
        if not signature or not hmac.compare_digest(_sign(self.key, body), signature):
            raise PackageError("package signature does not verify", location="signature")

        existing = self._package(signature)
        if existing is not None:
            raise PackageError(
                f"package already {existing['status'].lower()}; a voided package may "
                f"not be reused",
                location=package.get("package_id"),
            )

        # one sheet is pressed once, ever
        live = self.conn.execute(
            "SELECT session_id, status FROM sessions WHERE sheet_id = ? AND status != 'VOID'",
            (package["sheet_id"],),
        ).fetchone()
        if live is not None:
            raise CounterStateError(
                f"sheet {package['sheet_id']} already has a {live['status']} session "
                f"({live['session_id']}); a second set may not be pressed for the "
                f"same issuance",
                location=package["sheet_id"],
            )

        session_id = uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                "INSERT INTO packages(signature, package_json, status, received_at) VALUES (?,?,?,?)",
                (signature, json.dumps(package, ensure_ascii=False, sort_keys=True), "RECEIVED", _now()),
            )
            self.conn.execute(
                "INSERT INTO sessions(session_id, sheet_id, student_id, package_signature, status, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, package["sheet_id"], package["student_id"], signature, "LOADED", _now()),
            )
            for ordinal, serial in enumerate(package["stack"]):
                expected = package["expected_forms"][serial]
                self.conn.execute(
                    "INSERT INTO session_forms(session_id, ordinal, serial, recipient) VALUES (?,?,?,?)",
                    (session_id, ordinal, serial, expected["recipient"]),
                )
            self._action(session_id, "received", {"package_id": package.get("package_id")})
        return session_id

    # -- physical verification -----------------------------------------------------
    def verify_physicals(self, session_id: str) -> dict:
        """Check the physical slips in the cabinet against the package."""
        session = self._session(session_id)
        pkg = self._package(session["package_signature"])["package"]
        problems: List[dict] = []
        for serial in pkg["stack"]:
            expected = pkg["expected_forms"][serial]
            actual = self.cabinet.get(serial)
            if actual is None:
                problems.append({"serial": serial, "problem": "slip not in cabinet"})
                continue
            if serial in self.damaged:
                problems.append({"serial": serial, "problem": "slip is damaged"})
                continue
            if (actual.recipient != expected["recipient"]
                    or sorted(actual.shows) != expected["shows"]
                    or sorted(actual.blocks) != expected["blocks"]):
                problems.append({"serial": serial, "problem": "slip profile differs from package"})
        report = {"session_id": session_id, "ok": not problems, "problems": problems}
        with self.conn:
            self._action(session_id, "verify_physicals", report)
        return report

    # -- the press ------------------------------------------------------------------
    def press(self, session_id: str) -> dict:
        """Press the whole stack once.  Re-verifies physicals first."""
        session = self._session(session_id)
        if session["status"] != "LOADED":
            raise CounterStateError(
                f"session is {session['status']}, not LOADED; a sheet is pressed once",
                location=session_id,
            )
        check = self.verify_physicals(session_id)
        if not check["ok"]:
            raise CounterStateError(
                "physical verification failed; void the package and fetch a new one",
                location=session_id,
                detail=check["problems"],
            )
        pkg = self._package(session["package_signature"])["package"]

        # reconstruct the fields from the sealed package itself -- the counter
        # never touches the registry's grade store
        pairs = {
            cid: TermPair(
                pair_id=info["pair_id"], course_id=cid,
                zh=info["zh"], fl=info["fl"],
                zh_channel=info["zh_channel"], fl_channel=info["fl_channel"],
            )
            for cid, info in pkg["pair_registry"].items()
        }
        press_fields: List[PressField] = []
        for row in pkg["rows"]:
            pair = pairs[row["course_id"]]
            press_fields.append(PressField(row["record_id"], "zh", pair.zh_channel, pair.zh))
            press_fields.append(PressField(row["record_id"], "fl", pair.fl_channel, pair.fl))
            press_fields.append(PressField(row["record_id"], "pts", POINTS_CHANNEL, row["points"]))

        # press with the *actual* cabinet profiles
        stack = [self.cabinet[serial] for serial in pkg["stack"]]
        outcome = simulate_press(press_fields, stack)

        # the pressed imprints must equal the sealed expectation
        for serial in pkg["stack"]:
            expected = {
                (e["record_id"], e["field"]): e["value"]
                for e in pkg["expected_imprints"][serial]
            }
            if outcome.imprints_on(serial) != expected:
                raise CounterStateError(
                    f"pressed imprints on {serial} differ from the sealed package; "
                    f"aborting before any slip leaves the counter",
                    location=session_id,
                )

        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET status = 'PRESSED', pressed_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )
            self.conn.execute(
                "UPDATE packages SET status = 'CONSUMED' WHERE signature = ?",
                (session["package_signature"],),
            )
            for ordinal, serial in enumerate(pkg["stack"]):
                imprints = [
                    {"record_id": rid, "field": kind, "value": val}
                    for (rid, kind), val in sorted(outcome.imprints_on(serial).items())
                ]
                blocked = sorted(set(outcome.blocked_channels.get(serial, [])))
                self.conn.execute(
                    "UPDATE session_forms SET imprints_json = ?, blocked_json = ? "
                    "WHERE session_id = ? AND ordinal = ?",
                    (json.dumps(imprints, ensure_ascii=False),
                     json.dumps(blocked, ensure_ascii=False), session_id, ordinal),
                )
            self._action(session_id, "pressed", {
                "stack": pkg["stack"],
                "blocked_channels": {
                    serial: sorted(set(outcome.blocked_channels.get(serial, [])))
                    for serial in pkg["stack"]
                },
            })
        return {
            "session_id": session_id,
            "pressed": True,
            "imprints": {s: self._form_imprints(session_id, s) for s in pkg["stack"]},
        }

    # -- damage before pressing -----------------------------------------------------
    def report_damaged(self, session_id: str, serial: str) -> dict:
        """A slip of the loaded stack is damaged before pressing.

        Marks the slip damaged in the cabinet and voids the session and its
        package; the clerk must fetch a freshly sealed package (the registry
        re-plans with a spare).
        """
        session = self._session(session_id)
        if session["status"] != "LOADED":
            raise CounterStateError(
                "damage can only void a package before pressing",
                location=session_id,
            )
        self.damaged.add(serial)
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET status = 'VOID' WHERE session_id = ?", (session_id,)
            )
            self.conn.execute(
                "UPDATE packages SET status = 'VOID', voided_at = ?, void_reason = ? "
                "WHERE signature = ?",
                (_now(), f"slip {serial} damaged before pressing", session["package_signature"]),
            )
            receipt = self._action(session_id, "voided", {
                "reason": "damaged_slip", "serial": serial,
            })
        return {"session_id": session_id, "void": True, "damaged_serial": serial,
                "action": receipt}

    # -- distribution / receipts -----------------------------------------------------
    def distribute(self, session_id: str) -> dict:
        """Tear away the topmost not-yet-distributed slip."""
        session = self._session(session_id)
        if session["status"] != "PRESSED":
            raise CounterStateError(
                f"cannot distribute while session is {session['status']}",
                location=session_id,
            )
        row = self.conn.execute(
            "SELECT ordinal, serial, recipient FROM session_forms "
            "WHERE session_id = ? AND distributed_at IS NULL ORDER BY ordinal LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            raise CounterStateError("all slips already distributed", location=session_id)
        with self.conn:
            self.conn.execute(
                "UPDATE session_forms SET distributed_at = ? WHERE session_id = ? AND ordinal = ?",
                (_now(), session_id, row["ordinal"]),
            )
            receipt = self._action(session_id, "distributed", {
                "serial": row["serial"], "recipient": row["recipient"],
            })
        return {"session_id": session_id, "serial": row["serial"],
                "recipient": row["recipient"], "action": receipt}

    def stop(self, session_id: str) -> dict:
        session = self._session(session_id)
        if session["status"] != "PRESSED":
            raise CounterStateError(
                f"cannot stop a session in status {session['status']}",
                location=session_id,
            )
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET status = 'STOPPED', stopped_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )
            receipt = self._action(session_id, "stopped", {})
        return {"session_id": session_id, "stopped": True, "action": receipt}

    def resume(self, session_id: str) -> dict:
        """Relief clerk: continue from the retained serials and action receipts."""
        session = self._session(session_id)
        if session["status"] != "STOPPED":
            raise CounterStateError(
                f"cannot resume a session in status {session['status']}",
                location=session_id,
            )
        pending = self.pending_distributions(session_id)
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET status = 'PRESSED', stopped_at = NULL WHERE session_id = ?",
                (session_id,),
            )
            receipt = self._action(session_id, "resumed", {"pending_serials": pending})
        return {"session_id": session_id, "resumed": True,
                "pending_serials": pending, "action": receipt}

    def pending_distributions(self, session_id: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT serial FROM session_forms WHERE session_id = ? AND distributed_at IS NULL "
            "ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        return [r["serial"] for r in rows]

    def register_receipt(self, session_id: str, serial: str) -> dict:
        """Register the receipt for one distributed slip.

        The receipt contains only the fields that actually remain on that
        recipient's own slip -- nothing else leaves the counter, so a
        confidential field cannot leak back through a receipt.
        """
        session = self._session(session_id)
        if session["status"] != "PRESSED":
            raise CounterStateError(
                f"cannot register receipts while session is {session['status']}",
                location=session_id,
            )
        row = self.conn.execute(
            "SELECT ordinal, recipient, imprints_json, distributed_at, receipt_registered_at "
            "FROM session_forms WHERE session_id = ? AND serial = ?",
            (session_id, serial),
        ).fetchone()
        if row is None:
            raise CounterStateError(f"slip {serial!r} is not in this session", location=serial)
        if row["distributed_at"] is None:
            raise CounterStateError(
                f"slip {serial!r} has not been distributed yet", location=serial
            )
        if row["receipt_registered_at"] is not None:
            raise CounterStateError(
                f"receipt for slip {serial!r} is already registered", location=serial
            )
        receipt = {
            "recipient": row["recipient"],
            "serial": serial,
            "sheet_id": session["sheet_id"],
            "fields": json.loads(row["imprints_json"]),
        }
        with self.conn:
            self.conn.execute(
                "UPDATE session_forms SET receipt_registered_at = ? WHERE session_id = ? AND ordinal = ?",
                (_now(), session_id, row["ordinal"]),
            )
            self._action(session_id, "receipt_registered", {"serial": serial})
            remaining = self.conn.execute(
                "SELECT COUNT(*) AS n FROM session_forms WHERE session_id = ? AND "
                "(distributed_at IS NULL OR receipt_registered_at IS NULL)",
                (session_id,),
            ).fetchone()["n"]
            if remaining == 0:
                self.conn.execute(
                    "UPDATE sessions SET status = 'COMPLETED', completed_at = ? WHERE session_id = ?",
                    (_now(), session_id),
                )
                self._action(session_id, "completed", {})
        return receipt

    # -- status ------------------------------------------------------------------
    def _form_imprints(self, session_id: str, serial: str) -> List[dict]:
        row = self.conn.execute(
            "SELECT imprints_json FROM session_forms WHERE session_id = ? AND serial = ?",
            (session_id, serial),
        ).fetchone()
        return json.loads(row["imprints_json"]) if row and row["imprints_json"] else []

    def session_status(self, session_id: str) -> dict:
        session = self._session(session_id)
        forms = self.conn.execute(
            "SELECT ordinal, serial, recipient, imprints_json, blocked_json, distributed_at, "
            "receipt_registered_at FROM session_forms WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        actions = self.conn.execute(
            "SELECT action_id, kind, detail_json, at FROM actions WHERE session_id = ? "
            "ORDER BY action_id",
            (session_id,),
        ).fetchall()
        return {
            "session_id": session_id,
            "sheet_id": session["sheet_id"],
            "student_id": session["student_id"],
            "status": session["status"],
            "forms": [
                {
                    "serial": f["serial"],
                    "recipient": f["recipient"],
                    "imprints": json.loads(f["imprints_json"]) if f["imprints_json"] else None,
                    "distributed_at": f["distributed_at"],
                    "receipt_registered_at": f["receipt_registered_at"],
                }
                for f in forms
            ],
            "blocked_channels": {
                f["serial"]: json.loads(f["blocked_json"]) if f["blocked_json"] else []
                for f in forms
            },
            "actions": [
                {"action_id": a["action_id"], "kind": a["kind"],
                 "detail": json.loads(a["detail_json"]), "at": a["at"]}
                for a in actions
            ],
        }


# --------------------------------------------------------------------------- #
# permit-driven planning at the counter (alternative handover)
# --------------------------------------------------------------------------- #
def office_from_permit(permit: dict, *, key: bytes) -> TermOffice:
    """Rebuild an ephemeral terminology office from a signed permit.

    The permit carries full pair texts, channels and group approvals plus the
    cross-row constraints, so counter-side planning enforces exactly the same
    naming rules as the office itself.
    """
    if permit.get("format") != PERMIT_FORMAT:
        raise PackageError("not a term permit", location="format")
    body = {k: v for k, v in permit.items() if k not in ("signature", "permit_id")}
    if not hmac.compare_digest(_sign(key, body), permit.get("signature", "")):
        raise PackageError("permit signature does not verify", location="signature")
    office = TermOffice(":memory:")
    for cid, entry in permit["courses"].items():
        for p in entry["approved_pairs"]:
            office.register_pair(
                pair_id=p["pair_id"], course_id=cid, zh=p["zh"], fl=p["fl"],
                zh_channel=p["zh_channel"], fl_channel=p["fl_channel"],
                approved_groups=p["approved_groups"],
            )
    return office


__all__ = [
    "PACKAGE_FORMAT",
    "PERMIT_FORMAT",
    "RegistryOffice",
    "Counter",
    "office_from_permit",
]

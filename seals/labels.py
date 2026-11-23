"""Seal-label issuing service.

A named box label can only be made from a reagent run that actually started:
the run's reservations must be ``consumed`` rows against named seal-clay
bottles. A reservation that is still active (reserved only), a run that went
``released`` (expired before it could start / aborted), or a repeated scan can
never produce an extra label.

One portion presses one label strip. Each label therefore points at one
concrete consumed reservation and at the portion's ordinal within that
reservation's bottle. The label does not keep a private portion count: the
number of labels a run can ever make is exactly the number of portions its
consumed rows spent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from reagent.errors import Conflict, NotFoundError, ValidationError
from reagent.rules import advance_day, require_day, require_text
from reagent.schema import transaction


@dataclass(frozen=True)
class ConsumedPortion:
    reservation_id: str
    bottle_id: str
    portions: int
    # 1-based ordinal of the portion inside this bottle's reservation
    portion_seq: int


class SealLabelService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- issuing ------------------------------------------------------------
    def issue_label(
        self, label_id: str, box_id: str, run_id: str, day: int
    ) -> dict:
        """Press one label strip from one really-consumed portion of a run.

        Repeated scans never spend another portion: a box that already has a
        label gets its original label back, recorded as a ``duplicate`` scan.
        Every refusal is committed as an audit row before the domain error is
        raised, so failed scans survive even though no strip is pressed.
        """
        require_text(label_id, "label_id")
        require_text(box_id, "box_id")
        require_text(run_id, "run_id")
        require_day(day)
        refusal = self._check_refusal(label_id, box_id, run_id, day)
        if refusal is not None:
            outcome, detail, error = refusal
            # committed in its own transaction: a refused scan is a fact
            with transaction(self.conn):
                advance_day(self.conn, day)
                existing = self.conn.execute(
                    "SELECT label_id FROM labels WHERE box_id = ?", (box_id,)
                ).fetchone()
                self._record_scan(
                    box_id,
                    run_id,
                    day,
                    outcome,
                    existing["label_id"] if existing else None,
                    detail,
                )
            raise error
        with transaction(self.conn):
            advance_day(self.conn, day)
            run = self._require_run(run_id)
            if run["state"] != "started":
                # state changed between pre-flight and the write; refuse safely
                outcome = "released" if run["state"] == "released" else "reserved_only"
                self._record_scan(
                    box_id, run_id, day, outcome, None,
                    f"run {run_id} is {run['state']} at write time",
                )
                raise Conflict(f"run {run_id} cannot press a label while {run['state']}")
            portion = self._next_free_portion(run_id)
            if portion is None:
                # raced between the check and the write: record and refuse
                self._record_scan(
                    box_id, run_id, day, "insufficient", None,
                    f"every consumed portion of run {run_id} already backs a label",
                )
                raise Conflict(
                    f"run {run_id} has no unspent consumed portion left"
                )
            self.conn.execute(
                "INSERT INTO labels(label_id, box_id, run_id, reservation_id, "
                "portion_seq, issued_day, current_version) "
                "VALUES(?, ?, ?, ?, ?, ?, 1)",
                (
                    label_id,
                    box_id,
                    run_id,
                    portion.reservation_id,
                    portion.portion_seq,
                    day,
                ),
            )
            self._record_scan(
                box_id, run_id, day, "issued", label_id,
                f"pressed from {portion.bottle_id} portion {portion.portion_seq} "
                f"via reservation {portion.reservation_id}",
            )
            return self.get_label(label_id)

    def _check_refusal(
        self, label_id: str, box_id: str, run_id: str, day: int
    ) -> tuple[str, str, Conflict] | None:
        """Pre-flight checks outside the write transaction."""
        existing = self.conn.execute(
            "SELECT label_id FROM labels WHERE box_id = ?", (box_id,)
        ).fetchone()
        if existing is not None:
            detail = (
                f"box {box_id} already carries label {existing['label_id']}; "
                "repeat scan issues no second strip"
            )
            return (
                "duplicate",
                detail,
                Conflict(detail),
            )
        if self.conn.execute(
            "SELECT 1 FROM labels WHERE label_id = ?", (label_id,)
        ).fetchone():
            detail = f"label_id {label_id} already used"
            return ("duplicate", detail, Conflict(detail))
        run = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return None  # let the write path raise NotFoundError
        if run["state"] == "released":
            detail = (
                f"run {run_id} was released before it ever started "
                f"({run['failure_reason']})"
            )
            return (
                "released",
                detail,
                Conflict(
                    f"run {run_id} never started (released); no portion was "
                    "consumed, so no label can be pressed"
                ),
            )
        if run["state"] != "started":
            detail = f"run {run_id} is only reserved; start must succeed first"
            return (
                "reserved_only",
                detail,
                Conflict(
                    f"run {run_id} is {run['state']}: a reservation is not a "
                    "consumption, no label can be pressed"
                ),
            )
        return None

    def record_failed_scan(
        self, box_id: str, run_id: str, day: int, outcome: str, detail: str
    ) -> None:
        """Archive a scan that could not legally press a label."""
        if outcome not in (
            "reserved_only",
            "expired_unstarted",
            "insufficient",
            "released",
            "duplicate",
        ):
            raise ValidationError(f"unknown failed scan outcome {outcome!r}")
        with transaction(self.conn):
            advance_day(self.conn, day)
            label_id = None
            row = self.conn.execute(
                "SELECT label_id FROM labels WHERE box_id = ?", (box_id,)
            ).fetchone()
            if row is not None:
                label_id = row["label_id"]
            self._record_scan(box_id, run_id, day, outcome, label_id, detail)

    def get_label(self, label_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM labels WHERE label_id = ?", (label_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown label {label_id!r}")
        bottle = self.conn.execute(
            """
            SELECT b.bottle_id, b.kind FROM reservations r
              JOIN bottles b ON b.bottle_id = r.bottle_id
             WHERE r.reservation_id = ?
            """,
            (row["reservation_id"],),
        ).fetchone()
        return {
            "label_id": row["label_id"],
            "box_id": row["box_id"],
            "run_id": row["run_id"],
            "reservation_id": row["reservation_id"],
            "bottle_id": bottle["bottle_id"],
            "kind": bottle["kind"],
            "portion_seq": row["portion_seq"],
            "issued_day": row["issued_day"],
            "current_version": row["current_version"],
        }

    def list_labels(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT label_id FROM labels ORDER BY label_id"
        ).fetchall()
        return [self.get_label(r["label_id"]) for r in rows]

    def is_legal_label(self, label_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM labels WHERE label_id = ?", (label_id,)
        ).fetchone() is not None

    # -- helpers ------------------------------------------------------------
    def _require_run(self, run_id: str) -> sqlite3.Row:
        run = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise NotFoundError(f"unknown run {run_id!r}")
        return run

    def _next_free_portion(self, run_id: str) -> ConsumedPortion | None:
        """Find the first consumed portion not yet backing a label.

        A consumed reservation for N portions supplies N ordinal portions;
        every label claims one of them. Portions already claimed are derived
        from the labels table, never from a counter on the run.
        """
        rows = self.conn.execute(
            """
            SELECT r.reservation_id, r.bottle_id, r.portions
              FROM reservations r
             WHERE r.holder_id = ? AND r.status = 'consumed'
             ORDER BY r.reservation_id
            """,
            (run_id,),
        ).fetchall()
        used = {
            (r["reservation_id"], int(r["portion_seq"]))
            for r in self.conn.execute(
                "SELECT reservation_id, portion_seq FROM labels WHERE run_id = ?",
                (run_id,),
            )
        }
        for row in rows:
            for seq in range(1, int(row["portions"]) + 1):
                if (row["reservation_id"], seq) not in used:
                    return ConsumedPortion(
                        reservation_id=row["reservation_id"],
                        bottle_id=row["bottle_id"],
                        portions=int(row["portions"]),
                        portion_seq=seq,
                    )
        return None

    def _record_scan(
        self,
        box_id: str,
        run_id: str | None,
        day: int,
        outcome: str,
        label_id: str | None,
        detail: str,
    ) -> None:
        scan_id = self._scan_id(box_id, day)
        self.conn.execute(
            "INSERT INTO label_attempts(scan_id, box_id, run_id, day, outcome, "
            "label_id, detail) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (scan_id, box_id, run_id, day, outcome, label_id, detail),
        )

    def _scan_id(self, box_id: str, day: int) -> str:
        base = f"{box_id}#d{day}"
        candidate = base
        suffix = 1
        while self.conn.execute(
            "SELECT 1 FROM label_attempts WHERE scan_id = ?", (candidate,)
        ).fetchone() is not None:
            candidate = f"{base}~{suffix}"
            suffix += 1
        return candidate

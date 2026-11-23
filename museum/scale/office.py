"""The weighing room (天平室).

A plate (盘) is one weighing.  Every row of a plate is a ticket from the
:mod:`cs` ticket library; the ticket's amount column holds the number of
coins.  Rows are classified to mint periods -- stable catalog node ids -- by
the catalog committed at one cut, and the plate is *pinned* to that cut, so a
later ticket correction never rewrites an old appraisal.

Signing rules, exactly as the business prescribes:

* a withdrawn ticket row is not put on the plate;
* several catalog paths reaching the same period count that row only once;
* a row resolving to **zero** periods, or to **two or more distinct**
  periods, means the plate cannot be signed;
* each plate becomes one interval receipt
  ``lo <= sum(count[p] * mass[p]) <= hi`` over the shared period-mass CSP.

The office accepts multi-plate joint identification, rejects a receipt that
contradicts the receipts already on file (the contradictory receipt is never
stored as a valid appraisal), and answers compatibility questions with either
a concrete mass table or a named group of already conflicting receipts.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from cs.store import Store
from museum.weights import Constraint, WeightCSP, WeightError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scale_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq      INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    details  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_versions (
    version    INTEGER PRIMARY KEY,
    created_seq INTEGER NOT NULL,
    domains    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plates (
    plate_id        TEXT PRIMARY KEY,
    created_seq     INTEGER NOT NULL,
    state           TEXT NOT NULL,            -- signed | rejected
    catalog_revision TEXT NOT NULL,
    fact_revision   TEXT NOT NULL,
    fact_watermark  INTEGER NOT NULL,
    domain_version  INTEGER NOT NULL,
    lo              INTEGER,
    hi              INTEGER,
    detail          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    plate_id       TEXT PRIMARY KEY,
    created_seq    INTEGER NOT NULL,
    domain_version INTEGER NOT NULL,
    counts         TEXT NOT NULL,
    lo             INTEGER NOT NULL,
    hi             INTEGER NOT NULL,
    cut            TEXT NOT NULL,
    FOREIGN KEY (plate_id) REFERENCES plates (plate_id)
);
CREATE TABLE IF NOT EXISTS joint_reviews (
    review_id  TEXT PRIMARY KEY,
    created_seq INTEGER NOT NULL,
    plate_ids  TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    report     TEXT NOT NULL
);
"""


class ScaleError(Exception):
    """Base class for weighing-room rejections."""


class PlateError(ScaleError):
    """A plate cannot be signed (bad rows, duplicate plate, bad interval)."""


@dataclass(frozen=True)
class PlateRow:
    ticket_id: str
    amount: int
    direct_classes: tuple[str, ...]
    periods: tuple[str, ...]
    withdrawn: bool


@dataclass
class PlateResult:
    plate_id: str
    state: str
    cut: dict[str, Any]
    rows: list[PlateRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    interval: tuple[int, int] | None = None
    receipt_id: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plate_id": self.plate_id,
            "state": self.state,
            "cut": self.cut,
            "interval": list(self.interval) if self.interval else None,
            "counts": dict(sorted(self.counts.items())),
            "receipt_id": self.receipt_id,
            "rows": [
                {
                    "ticket_id": r.ticket_id,
                    "amount": r.amount,
                    "classes": list(r.direct_classes),
                    "periods": list(r.periods),
                    "withdrawn": r.withdrawn,
                }
                for r in self.rows
            ],
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ScaleOffice:
    """SQLite-backed weighing room working against one :class:`Store`."""

    def __init__(self, connection: sqlite3.Connection, store: Store) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.store = store

    @classmethod
    def open(cls, path: str, store: Store) -> "ScaleOffice":
        conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return cls(conn, store)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ScaleOffice":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ internals
    def _next_seq(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM scale_events"
        ).fetchone()
        return int(row["n"])

    def _record(self, seq: int, kind: str, details: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO scale_events(seq, kind, details) VALUES(?,?,?)",
            (seq, kind, _json(details)),
        )

    def _current_domain_version(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM domain_versions"
        ).fetchone()
        return int(row["v"])

    def _load_domains(self, version: int | None = None) -> dict[str, tuple[int, ...]]:
        version = version if version is not None else self._current_domain_version()
        row = self.conn.execute(
            "SELECT domains FROM domain_versions WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise ScaleError("no mass domains have been declared")
        raw = json.loads(row["domains"])
        return {period: tuple(masses) for period, masses in raw.items()}

    # ------------------------------------------------------------- registry
    def declare_domains(
        self, domains: Mapping[str, Sequence[int]]
    ) -> int:
        """Declare the legal finite mass set of every mint period.

        Re-declaring identical domains is an idempotent no-op; any change
        opens a new version.  Old receipts keep the version they were signed
        against, so historical appraisals stay reproducible.
        """

        clean = {
            period: tuple(sorted(set(int(m) for m in masses)))
            for period, masses in sorted(domains.items())
        }
        for period, masses in clean.items():
            if not masses or any(m <= 0 for m in masses):
                raise WeightError(
                    f"period {period!r} needs positive integer legal masses"
                )
        version = self._current_domain_version()
        if version:
            current = self._load_domains(version)
            if current == clean:
                return version
        new_version = version + 1
        seq = self._next_seq()
        self.conn.execute(
            "INSERT INTO domain_versions(version, created_seq, domains) VALUES(?,?,?)",
            (new_version, seq, _json(clean)),
        )
        self._record(seq, "domains", {"version": new_version, "domains": clean})
        return new_version

    # ------------------------------------------------------------- resolution
    def _resolve_rows(
        self, ticket_ids: Sequence[str], cut
    ) -> tuple[list[PlateRow], list[str]]:
        """Classify each ticket row to the distinct periods it reaches.

        One multi-target query at a single cut resolves every period at once.
        A ticket maps to a period when any of its direct classes can reach
        that period; several paths never multiply the row.
        """

        domains = self._load_domains()
        periods = sorted(domains)
        if not periods:
            raise ScaleError("cannot sign a plate before mass domains are declared")
        result = self.store.query(periods, snapshot=cut)
        members: dict[str, set[str]] = {period: set() for period in periods}
        for group in result["groups"]:
            for contribution in group["contributions"]:
                members[group["target_id"]].add(contribution["ticket_id"])

        # Ticket content at this exact cut, plus the withdrawn revision set.
        facts = {fact.ticket_id: fact for fact in self.store.load_facts_at_cut(cut)}
        withdrawn = self.store.withdrawn_tickets_at_cut(cut)

        rows: list[PlateRow] = []
        errors: list[str] = []
        seen: set[str] = set()
        for ticket_id in ticket_ids:
            if ticket_id in seen:
                errors.append(f"ticket {ticket_id} appears twice on one plate")
                continue
            seen.add(ticket_id)
            reached = tuple(
                sorted(period for period in periods if ticket_id in members[period])
            )
            fact = facts.get(ticket_id)
            if fact is not None and ticket_id not in withdrawn:
                rows.append(
                    PlateRow(
                        ticket_id=ticket_id,
                        amount=fact.amount,
                        direct_classes=tuple(fact.classes),
                        periods=reached,
                        withdrawn=False,
                    )
                )
            elif ticket_id in withdrawn:
                old = withdrawn[ticket_id]
                rows.append(
                    PlateRow(
                        ticket_id=ticket_id,
                        amount=old.amount,
                        direct_classes=tuple(old.classes),
                        periods=(),
                        withdrawn=True,
                    )
                )
            else:
                errors.append(f"ticket {ticket_id} is not registered in the ticket library")
                continue
            if rows[-1].withdrawn:
                continue  # withdrawn rows are not put on the plate
            if len(reached) == 0:
                errors.append(
                    f"ticket {ticket_id} reaches no declared mint period; plate cannot be signed"
                )
            elif len(reached) > 1:
                errors.append(
                    f"ticket {ticket_id} reaches multiple distinct periods "
                    f"{list(reached)}; plate cannot be signed"
                )
        return rows, errors

    # ------------------------------------------------------------- sign plate
    def sign_plate(
        self,
        plate_id: str,
        ticket_ids: Sequence[str],
        interval: tuple[int, int],
    ) -> PlateResult:
        """Sign one plate and admit its receipt, or reject the whole plate.

        A rejection leaves no valid receipt behind and is recorded as a
        rejected-plate event, so the refusal is auditable but cannot poison
        later joint identification.
        """

        existing = self.conn.execute(
            "SELECT state, detail FROM plates WHERE plate_id=?", (plate_id,)
        ).fetchone()
        lo, hi = interval
        if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi or lo < 0:
            raise PlateError(f"plate {plate_id!r}: interval must be 0 <= lo <= hi")

        cut = self.store.current_cut()
        rows, errors = self._resolve_rows(ticket_ids, cut)
        loaded = [r for r in rows if not r.withdrawn]
        counts: dict[str, int] = {}
        for row in loaded:
            if len(row.periods) == 1:
                counts[row.periods[0]] = counts.get(row.periods[0], 0) + row.amount
        warnings = [
            f"withdrawn row not placed on plate: {r.ticket_id}"
            for r in rows
            if r.withdrawn
        ]

        base = PlateResult(
            plate_id=plate_id,
            state="rejected",
            cut=cut.as_dict(),
            rows=rows,
            counts=counts,
            interval=(lo, hi),
            errors=list(errors),
            warnings=warnings,
        )

        if existing is not None:
            prior = json.loads(existing["detail"])
            candidate = base.as_dict()
            # The stored detail already carries the final state; compare the
            # structural fields so an identical replay returns the stored plate
            # without demanding byte-identical error ordering.
            comparable = (
                "cut", "counts", "interval", "rows", "warnings", "errors",
            )
            same = all(prior.get(k) == candidate.get(k) for k in comparable)
            if same:
                return PlateResult(
                    plate_id=plate_id,
                    state=existing["state"],
                    cut=cut.as_dict(),
                    rows=rows,
                    counts=counts,
                    interval=(lo, hi),
                    errors=errors,
                    warnings=warnings,
                    receipt_id=plate_id if existing["state"] == "signed" else None,
                )
            raise PlateError(f"plate {plate_id!r} already exists with different content")

        if errors:
            self._persist_plate(base, signed=False)
            return base
        if not counts:
            base.errors.append(
                "plate has no countable coin rows (every row was withdrawn or rejected)"
            )
            self._persist_plate(base, signed=False)
            return base

        csp = self._build_csp()
        if not csp.constraints:
            # The first plate establishes the appraisal; just verify the
            # interval is physically possible under the declared domains.
            trial = self._build_csp(include_plate=(counts, lo, hi))
            if trial.solve() is None:
                base.errors.append(
                    "no legal mass assignment can reach the stated interval for this plate"
                )
                self._persist_plate(base, signed=False)
                return base
        else:
            established = csp.solve()
            if established is None:
                # Earlier receipts already contradict each other: no new
                # receipt can be admitted until the conflict is resolved.
                core = csp.conflict_core()
                base.errors.append(
                    "the receipts already on file are mutually contradictory; "
                    f"conflicting receipts: {[c.receipt_id for c in core]}"
                )
                self._persist_plate(base, signed=False)
                return base
            # The office has already published one concrete witness mass
            # table (the deterministic first solution of the receipts on
            # file).  For periods those receipts actually weighed, the new
            # plate must fit the established masses -- the office never
            # silently switches a weighed mass to another value.  Periods
            # that no earlier receipt ever weighed are unconstrained and may
            # take any legal value, contributing their full domain range.
            trial = self._build_csp(include_plate=(counts, lo, hi))
            joint_witness = trial.solve()
            historically_weighed = {
                period
                for constraint in csp.constraints
                for period in constraint.counts
            }
            pinned_weight = sum(
                counts[p] * established[p]
                for p in counts
                if p in historically_weighed
            )
            free_min = sum(
                counts[p] * csp.domains[p][0]
                for p in counts
                if p not in historically_weighed
            )
            free_max = sum(
                counts[p] * csp.domains[p][-1]
                for p in counts
                if p not in historically_weighed
            )
            reachable_lo, reachable_hi = pinned_weight + free_min, pinned_weight + free_max
            supported = not (reachable_hi < lo or reachable_lo > hi)
            established_for_pending = {
                p: established[p] for p in counts if p in historically_weighed
            }
            if not supported:
                core = trial.conflict_core() if joint_witness is None else []
                base.errors.append(
                    "receipt contradicts the established mass interpretation "
                    f"{established_for_pending}; refusing rather than flipping an "
                    "old appraisal."
                )
                base.errors.append(
                    "established_masses: "
                    + json.dumps(established_for_pending, ensure_ascii=False)
                )
                if core:
                    base.errors.append(
                        "joint_conflict_core: "
                        + json.dumps([c.as_dict() for c in core], ensure_ascii=False)
                    )
                self._persist_plate(base, signed=False)
                return base

        base.state = "signed"
        base.receipt_id = plate_id
        self._persist_plate(base, signed=True, counts=counts, lo=lo, hi=hi)
        return base

    def _persist_plate(
        self,
        result: PlateResult,
        *,
        signed: bool,
        counts: dict[str, int] | None = None,
        lo: int | None = None,
        hi: int | None = None,
    ) -> None:
        seq = self._next_seq()
        version = self._current_domain_version()
        self.conn.execute(
            "INSERT INTO plates(plate_id, created_seq, state, catalog_revision, "
            "fact_revision, fact_watermark, domain_version, lo, hi, detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                result.plate_id,
                seq,
                "signed" if signed else "rejected",
                result.cut["catalog_revision"],
                result.cut["fact_revision"],
                result.cut["fact_watermark"],
                version,
                lo if signed else None,
                hi if signed else None,
                _json(result.as_dict()),
            ),
        )
        if signed:
            assert counts is not None and lo is not None and hi is not None
            self.conn.execute(
                "INSERT INTO receipts(plate_id, created_seq, domain_version, "
                "counts, lo, hi, cut) VALUES(?,?,?,?,?,?,?)",
                (
                    result.plate_id,
                    seq,
                    version,
                    _json(counts),
                    lo,
                    hi,
                    _json(result.cut),
                ),
            )
        self._record(
            seq,
            "plate_signed" if signed else "plate_rejected",
            result.as_dict(),
        )

    # ------------------------------------------------------------- CSP views
    def _build_csp(
        self,
        *,
        include_plate: tuple[dict[str, int], int, int] | None = None,
        plate_ids: Sequence[str] | None = None,
    ) -> WeightCSP:
        csp = WeightCSP()
        version = self._current_domain_version()
        for period, masses in self._load_domains(version).items():
            csp.declare_period(period, masses)
        rows = self.conn.execute(
            "SELECT plate_id, counts, lo, hi FROM receipts ORDER BY plate_id"
        ).fetchall()
        wanted = set(plate_ids) if plate_ids is not None else None
        for row in rows:
            if wanted is not None and row["plate_id"] not in wanted:
                continue
            csp.add_constraint(
                row["plate_id"], json.loads(row["counts"]), int(row["lo"]), int(row["hi"])
            )
        if include_plate is not None:
            counts, lo, hi = include_plate
            csp.add_constraint("__pending__", counts, lo, hi)
        return csp

    def joint_appraisal(self, plate_ids: Sequence[str]) -> dict[str, Any]:
        """Multi-plate joint identification over one shared mass assignment."""

        ids = list(dict.fromkeys(plate_ids))
        known = {
            row["plate_id"]
            for row in self.conn.execute("SELECT plate_id FROM receipts").fetchall()
        }
        unknown = [pid for pid in ids if pid not in known]
        if unknown:
            raise ScaleError(f"not signed receipts: {unknown}")
        csp = self._build_csp(plate_ids=ids)
        report = csp.query()
        report["plate_ids"] = ids
        report["verdict"] = "compatible" if report["satisfiable"] else "contradictory"
        seq = self._next_seq()
        review_id = "JR-" + str(seq)
        self.conn.execute(
            "INSERT INTO joint_reviews(review_id, created_seq, plate_ids, verdict, report) "
            "VALUES(?,?,?,?,?)",
            (review_id, seq, _json(ids), report["verdict"], _json(report)),
        )
        self._record(seq, "joint_review", {"review_id": review_id, **report})
        report["review_id"] = review_id
        return report

    def compatibility(self, periods: Sequence[str] | None = None) -> dict[str, Any]:
        """Do the proposed period masses fit *all* receipts at the same time?"""

        csp = self._build_csp()
        report = csp.query(periods=list(periods) if periods else None)
        report["receipt_count"] = len(csp.constraints)
        return report

    def check_mass_table(self, masses: Mapping[str, int]) -> dict[str, Any]:
        """Verify one explicitly proposed weight interpretation end to end."""

        csp = self._build_csp()
        broken = csp.check_fixed(dict(masses))
        return {
            "supported": not broken,
            "masses": dict(sorted(masses.items())),
            "violated_receipts": [c.receipt_id for c in broken],
            "violation_detail": [c.as_dict() for c in broken],
        }

    # ------------------------------------------------------------- read side
    def get_plate(self, plate_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT detail FROM plates WHERE plate_id=?", (plate_id,)
        ).fetchone()
        if row is None:
            raise ScaleError(f"unknown plate {plate_id!r}")
        return json.loads(row["detail"])

    def list_receipts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT plate_id, counts, lo, hi, cut, domain_version "
            "FROM receipts ORDER BY plate_id"
        ).fetchall()
        return [
            {
                "receipt_id": row["plate_id"],
                "counts": json.loads(row["counts"]),
                "interval": [int(row["lo"]), int(row["hi"])],
                "cut": json.loads(row["cut"]),
                "domain_version": int(row["domain_version"]),
            }
            for row in rows
        ]

    def history(self) -> list[dict[str, Any]]:
        return [
            {"seq": int(r["seq"]), "kind": r["kind"], "details": json.loads(r["details"])}
            for r in self.conn.execute(
                "SELECT seq, kind, details FROM scale_events ORDER BY seq, event_id"
            ).fetchall()
        ]

    def constraints_for(self, plate_ids: Sequence[str]) -> list[Constraint]:
        csp = self._build_csp(plate_ids=list(plate_ids))
        return csp.constraints

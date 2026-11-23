"""Glazing workshop store, reusing the ``cs`` catalog and cut machinery.

Why this reuses ``cs``
----------------------
The frame grooves are catalog nodes and a survey's *direct classes* are the
grooves it was filed under.  To learn which groove a survey belongs to we walk
the committed catalog upward exactly as ``cs`` does: a ticket counts once for a
groove if any direct class reaches it, no matter how many classes or paths lead
there.  Zero reachable grooves, or more than one, cannot be cut.

What is glazing-specific
------------------------
* survey revisions, signing rounds and withdrawal live here, because "highest
  active round per slot/position/item" and "withdrawal re-exposes the previous
  slip" are workshop rules, not generic summary rules;
* the bench orientation is undone before storage (see :mod:`glass.geom`);
* a signing slip is pinned to a catalog/fact/watermark cut and is immutable;
  later bead changes never rewrite it;
* stock, cut planning and physical handoff live here too.

All tables sit in the same SQLite file as the ``cs`` tables, so one connection
and one read/cut discipline serve both.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from cs import catalog
from cs.store import CatalogError, Store, StoreError, _json_dump, _Transaction, _utc_now

from . import fit
from .cutting import Defect, Ledger, PaneSpec, Piece, Plan, plan_cuts

SCHEMA = """
CREATE TABLE IF NOT EXISTS glass_slots (
    slot_id     TEXT PRIMARY KEY,
    color       TEXT NOT NULL,
    grained     INTEGER NOT NULL DEFAULT 0,
    description TEXT
);
CREATE TABLE IF NOT EXISTS glass_surveys (
    survey_id   TEXT PRIMARY KEY,
    ticket_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    direct_classes TEXT NOT NULL,
    withdrawn   INTEGER NOT NULL DEFAULT 0,
    cut_catalog TEXT NOT NULL,
    cut_fact    TEXT NOT NULL,
    cut_watermark INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    UNIQUE (ticket_id, revision)
);
CREATE TABLE IF NOT EXISTS glass_readings (
    survey_id        TEXT NOT NULL,
    ticket_id        TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    slot             TEXT NOT NULL,
    install_position TEXT NOT NULL,
    item             TEXT NOT NULL,
    target           TEXT NOT NULL,
    payload          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glass_signings (
    signing_id     TEXT PRIMARY KEY,
    slot_id        TEXT NOT NULL,
    position       TEXT NOT NULL,
    color          TEXT NOT NULL,
    grain          TEXT,
    sizes          TEXT NOT NULL,
    cut_catalog    TEXT NOT NULL,
    cut_fact       TEXT NOT NULL,
    cut_watermark  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glass_stock (
    batch_id       TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    remaining_cuts INTEGER NOT NULL,
    pieces         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glass_plans (
    plan_id        TEXT PRIMARY KEY,
    batch_id       TEXT NOT NULL,
    signing_ids    TEXT NOT NULL,
    cuts_used      INTEGER NOT NULL,
    payload        TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""


class GlassError(StoreError):
    """A glazing-domain rejection."""


@dataclass
class Slot:
    slot_id: str
    color: str
    grained: bool
    description: str | None = None

    def as_dict(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "color": self.color,
            "grained": self.grained,
            "description": self.description,
        }


@dataclass
class SurveyResolution:
    survey_id: str
    ticket_id: str
    revision: int
    reachable: list[str]
    contributions: dict[str, list[dict]]
    slot: str | None
    usable: bool
    reason: str | None

    def as_dict(self) -> dict:
        return {
            "survey_id": self.survey_id,
            "ticket_id": self.ticket_id,
            "revision": self.revision,
            "reachable": self.reachable,
            "contributions": self.contributions,
            "slot": self.slot,
            "usable": self.usable,
            "reason": self.reason,
        }


class GlazingStore:
    """Glazing rules over a shared :class:`cs.store.Store`."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.conn: sqlite3.Connection = store.conn
        self.conn.executescript(SCHEMA)

    @classmethod
    def open(cls, path: str) -> "GlazingStore":
        return cls(Store.open(path))

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "GlazingStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- slots
    def define_slot(
        self,
        slot_id: str,
        color: str,
        grained: bool = False,
        description: str | None = None,
        *,
        name: str | None = None,
        ensure_node: bool = True,
    ) -> Slot:
        """Register a frame groove (and, by default, its catalog node)."""

        slot = Slot(slot_id, color, grained, description)
        if ensure_node and slot_id not in self.store.load_catalog_state().nodes:
            self.store.add_nodes(
                [{"id": slot_id, "name": name or slot_id}],
                request_id=f"slot-node-{slot_id}",
            )
        with self._lock(), _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO glass_slots(slot_id, color, grained, description) "
                "VALUES(?,?,?,?) ON CONFLICT(slot_id) DO UPDATE SET "
                "color=excluded.color, grained=excluded.grained, "
                "description=excluded.description",
                (slot.slot_id, slot.color, int(slot.grained), slot.description),
            )
        return slot

    def get_slot(self, slot_id: str) -> Slot:
        row = self.conn.execute(
            "SELECT * FROM glass_slots WHERE slot_id=?", (slot_id,)
        ).fetchone()
        if row is None:
            raise GlassError(f"unknown slot {slot_id!r}")
        return Slot(row["slot_id"], row["color"], bool(row["grained"]), row["description"])

    def slots(self) -> list[Slot]:
        rows = self.conn.execute("SELECT * FROM glass_slots ORDER BY slot_id").fetchall()
        return [Slot(r["slot_id"], r["color"], bool(r["grained"]), r["description"])
                for r in rows]

    def _slot_ids(self) -> set[str]:
        return {r["slot_id"] for r in
                self.conn.execute("SELECT slot_id FROM glass_slots").fetchall()}

    # ------------------------------------------------------------- watermarks
    def _meta(self, key: str, default: int = 0) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return int(row["value"]) if row else default

    def _bump_glass_watermark(self) -> int:
        """Advance the glazing watermark; returns the new committed value."""

        nxt = self._meta("glass_watermark", 0) + 1
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('glass_watermark', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(nxt),),
        )
        return nxt

    def glass_watermark(self) -> int:
        return self._meta("glass_watermark", 0)

    def current_glass_cut(self) -> dict:
        return {
            "catalog_revision": self.store.catalog_revision() or "G0",
            "glass_watermark": self.glass_watermark(),
        }

    # ---------------------------------------------------------- measurements
    def submit_quantity(
        self,
        survey_id: str,
        ticket_id: str,
        revision: int,
        direct_classes: Sequence[str],
        measurements: Sequence[Mapping[str, Any]],
        *,
        amount: int | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Record one survey revision (idempotent; rejects stale/conflicting)."""

        result = self.submit_quantities(
            [
                {
                    "survey_id": survey_id,
                    "ticket_id": ticket_id,
                    "revision": revision,
                    "direct_classes": list(direct_classes),
                    "measurements": list(measurements),
                    "amount": amount,
                }
            ]
        )
        return {
            "survey_id": survey_id,
            "ticket_id": ticket_id,
            "revision": revision,
            "cut": result["cut"],
            "noop": result["noop"],
        }

    def submit_quantities(self, surveys: Sequence[Mapping[str, Any]]) -> dict:
        """Record several survey slips as one atomic revision unit.

        This is the glazing analogue of a ``cs`` bundle: every slip shares one
        commit, so e.g. the two slips that together change the left slot's right
        lap become visible together, never one before the other.  Any conflict
        aborts the whole batch before a single row is written.
        """

        if not surveys:
            raise GlassError("submit_quantities needs at least one survey")
        state = self.store.load_catalog_state()
        prepared = []
        seen_tickets: dict[str, int] = {}
        for raw in surveys:
            survey_id = raw["survey_id"]
            ticket_id = raw["ticket_id"]
            revision = raw["revision"]
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise GlassError("revision must be an integer >= 1")
            amount = raw.get("amount")
            if amount is not None and (
                not isinstance(amount, int) or isinstance(amount, bool) or amount < 0
            ):
                raise GlassError("amount must be a non-negative integer")
            classes = list(raw["direct_classes"])
            if not classes:
                raise GlassError("a survey needs at least one direct class")
            unknown = [c for c in classes if c not in state.nodes]
            if unknown:
                raise CatalogError(
                    f"survey files unknown classes: {', '.join(sorted(unknown))}"
                )
            if ticket_id in seen_tickets and seen_tickets[ticket_id] != revision:
                raise GlassError(f"ticket {ticket_id}: two revisions in one batch")
            seen_tickets[ticket_id] = revision
            normalized = []
            for m in raw.get("measurements", []):
                normalized.extend(fit.normalize_quantity(m))
            _check_no_same_round_conflict(normalized)
            prepared.append(
                (survey_id, ticket_id, revision, sorted(set(classes)), raw, normalized)
            )

        with self._lock(), _Transaction(self.conn):
            # Validate every slip against the committed survey history first.
            conflicts = []
            duplicates: dict[tuple[str, int], bool] = {}
            for survey_id, ticket_id, revision, classes, raw, normalized in prepared:
                payload = self._survey_payload(
                    survey_id, ticket_id, revision, classes, raw, normalized
                )
                existing = self.conn.execute(
                    "SELECT payload, withdrawn FROM glass_surveys "
                    "WHERE ticket_id=? AND revision=?",
                    (ticket_id, revision),
                ).fetchone()
                if existing is not None:
                    if json.loads(existing["payload"]) != payload:
                        conflicts.append(
                            f"ticket {ticket_id}: revision {revision} already "
                            "committed with different content; re-take the measurement"
                        )
                    elif int(existing["withdrawn"]):
                        conflicts.append(
                            f"ticket {ticket_id}: revision {revision} was withdrawn; "
                            "use a higher revision"
                        )
                    duplicates[(ticket_id, revision)] = True
                    continue
                newer = self.conn.execute(
                    "SELECT MAX(revision) AS m FROM glass_surveys WHERE ticket_id=?",
                    (ticket_id,),
                ).fetchone()["m"]
                if newer is not None and revision < int(newer):
                    conflicts.append(
                        f"ticket {ticket_id}: revision {revision} is older than {newer}"
                    )
            if conflicts:
                raise GlassError("; ".join(conflicts))

            watermark = self._bump_glass_watermark()
            cut = {
                "catalog_revision": self.store.catalog_revision() or "G0",
                "glass_watermark": watermark,
            }
            committed: list[str] = []
            for survey_id, ticket_id, revision, classes, raw, normalized in prepared:
                if (ticket_id, revision) in duplicates:
                    continue
                payload = self._survey_payload(
                    survey_id, ticket_id, revision, classes, raw, normalized
                )
                self.conn.execute(
                    "INSERT INTO glass_surveys"
                    "(survey_id, ticket_id, revision, direct_classes, withdrawn, "
                    "cut_catalog, cut_fact, cut_watermark, created_at, payload)"
                    " VALUES(?,?,?,?,0,?,?,?,?,?)",
                    (
                        survey_id, ticket_id, revision, _json_dump(classes),
                        cut["catalog_revision"], "F0", watermark,
                        _utc_now(), _json_dump(payload),
                    ),
                )
                for r in normalized:
                    self.conn.execute(
                        "INSERT INTO glass_readings"
                        "(survey_id, ticket_id, revision, slot, install_position, "
                        "item, target, payload) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            survey_id, ticket_id, revision, r.slot,
                            r.install_position, r.item, r.target,
                            _json_dump(list(r.payload)),
                        ),
                    )
                committed.append(survey_id)
        return {"committed": committed, "cut": cut,
                "noop": not committed}

    @staticmethod
    def _survey_payload(
        survey_id: str,
        ticket_id: str,
        revision: int,
        classes: Sequence[str],
        raw: Mapping[str, Any],
        normalized: Sequence[fit.Reading],
    ) -> dict:
        return {
            "survey_id": survey_id,
            "ticket_id": ticket_id,
            "revision": revision,
            "direct_classes": sorted(set(classes)),
            "amount": raw.get("amount"),
            "measurements": list(raw.get("measurements", [])),
            "readings": [
                {
                    "slot": r.slot,
                    "install_position": r.install_position,
                    "item": r.item,
                    "target": r.target,
                    "payload": list(r.payload),
                    "revision": r.revision,
                }
                for r in normalized
            ],
        }

    def withdraw_quantity(
        self,
        ticket_id: str,
        expected_revision: int | None = None,
    ) -> dict:
        """Withdraw the ticket's current signing round; expose the previous one.

        The withdrawn survey stays in history but no longer contributes
        readings, so the next highest non-withdrawn revision of the same ticket
        becomes effective again.
        """

        with self._lock(), _Transaction(self.conn):
            rows = self.conn.execute(
                "SELECT survey_id, revision, withdrawn FROM glass_surveys "
                "WHERE ticket_id=? ORDER BY revision DESC",
                (ticket_id,),
            ).fetchall()
            if not rows:
                raise GlassError(f"ticket {ticket_id} has no survey")
            active = [r for r in rows if not int(r["withdrawn"])]
            if not active:
                raise GlassError(f"ticket {ticket_id} is already fully withdrawn")
            current = active[0]
            if expected_revision is not None and int(current["revision"]) != expected_revision:
                raise GlassError(
                    f"ticket {ticket_id}: expected active revision "
                    f"{expected_revision}, current active is {current['revision']}"
                )
            self.conn.execute(
                "UPDATE glass_surveys SET withdrawn=1 WHERE survey_id=?",
                (current["survey_id"],),
            )
            remaining = [r for r in active[1:]]
            exposed = remaining[0]["revision"] if remaining else None
        return {
            "ticket_id": ticket_id,
            "withdrawn_revision": int(current["revision"]),
            "exposed_revision": int(exposed) if exposed is not None else None,
        }

    def surveys(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM glass_surveys ORDER BY ticket_id, revision"
        ).fetchall()
        return [
            {
                "survey_id": r["survey_id"],
                "ticket_id": r["ticket_id"],
                "revision": int(r["revision"]),
                "direct_classes": json.loads(r["direct_classes"]),
                "withdrawn": bool(r["withdrawn"]),
                "cut": {
                    "catalog_revision": r["cut_catalog"],
                    "glass_watermark": int(r["cut_watermark"]),
                },
            }
            for r in rows
        ]

    def _effective_survey(self, ticket_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM glass_surveys WHERE ticket_id=? AND withdrawn=0 "
            "ORDER BY revision DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()

    def reading_set(self) -> dict[tuple, fit.Reading]:
        """Highest active round per (resolved slot, position, item, target).

        A reading is attributed to a groove by *resolving its survey through the
        committed catalog* — never by a free-text tag — so an ambiguous or
        zero-reach survey contributes nothing to cutting.  The rule is per
        measurement, not per slip: a newer slip that only re-measured the right
        lap leaves the older top/left/grain readings in force.  Withdrawal marks
        a slip inactive, so its readings drop out and the previous round is
        exposed again.
        """

        raw = []
        rows = self.conn.execute(
            "SELECT * FROM glass_surveys WHERE withdrawn=0 "
            "ORDER BY ticket_id, revision"
        ).fetchall()
        for survey in rows:
            slot = self._resolve_classes(json.loads(survey["direct_classes"]))
            if slot is None:
                continue
            payload = json.loads(survey["payload"])
            for r in payload.get("readings", []):
                raw.append(
                    {
                        "slot": slot,
                        "install_position": r["install_position"],
                        "item": r["item"],
                        "target": r["target"],
                        "revision": r["revision"],
                        "payload": r["payload"],
                    }
                )
        return _select_from_normalized(raw)

    # ----------------------------------------------------------- resolution
    def _reachable_slots(
        self, classes: Sequence[str]
    ) -> tuple[list[str], dict[str, list[dict]]]:
        """Grooves reachable from ``classes`` plus one witness path each."""

        classes = tuple(classes)
        state = self.store.load_catalog_state()
        slot_ids = self._slot_ids()
        contributions: dict[str, list[dict]] = {}
        for target in sorted(state.nodes):
            if target not in slot_ids:
                continue
            dist, next_hop = catalog.distances_to_target(target, state.edges)
            path = catalog.choose_path(classes, target, dist, next_hop)
            if path is None:
                continue
            # One witness per groove even with many classes/paths.
            contributions.setdefault(target, []).append({"path": path})
        return sorted(contributions), contributions

    def _resolve_classes(self, classes: Sequence[str]) -> str | None:
        """The single groove a class set files to, or ``None`` if not unique."""

        reachable, _ = self._reachable_slots(classes)
        return reachable[0] if len(reachable) == 1 else None

    def resolve_survey(self, survey_id: str) -> SurveyResolution:
        """Resolve one survey to a groove using the committed catalog."""

        row = self.conn.execute(
            "SELECT * FROM glass_surveys WHERE survey_id=?", (survey_id,)
        ).fetchone()
        if row is None:
            raise GlassError(f"unknown survey {survey_id!r}")
        if int(row["withdrawn"]):
            return SurveyResolution(
                survey_id, row["ticket_id"], int(row["revision"]), [], {}, None, False,
                "survey revision is withdrawn; no active classification",
            )
        effective = self._effective_survey(row["ticket_id"])
        if effective is None or effective["survey_id"] != survey_id:
            return SurveyResolution(
                survey_id, row["ticket_id"], int(row["revision"]), [], {}, None, False,
                "a higher signing round is active for this ticket",
            )
        reachable, base = self._reachable_slots(
            json.loads(row["direct_classes"])
        )
        contributions = {
            groove: [{"ticket_id": row["ticket_id"], "path": e["path"]} for e in entries]
            for groove, entries in base.items()
        }
        if len(reachable) == 1:
            return SurveyResolution(
                survey_id, row["ticket_id"], int(row["revision"]),
                reachable, contributions, reachable[0], True, None,
            )
        if not reachable:
            reason = "no frame groove is reachable from the filed classes"
        else:
            reason = (
                "survey reaches several grooves ("
                + ", ".join(reachable)
                + "); a ticket that resolves to more than one cannot cut"
            )
        return SurveyResolution(
            survey_id, row["ticket_id"], int(row["revision"]),
            reachable, contributions, None, False, reason,
        )

    def resolve_all(self) -> list[SurveyResolution]:
        rows = self.conn.execute(
            "SELECT survey_id FROM glass_surveys ORDER BY survey_id"
        ).fetchall()
        return [self.resolve_survey(r["survey_id"]) for r in rows]

    # ------------------------------------------------------------- signings
    def requirement_for(
        self,
        slot_id: str,
        position: str = "single",
        readings: Mapping[tuple, fit.Reading] | None = None,
    ) -> fit.PositionRequirement:
        readings = readings if readings is not None else self.reading_set()
        slot = self.get_slot(slot_id)
        return fit.build_requirement(slot_id, slot.color, position, readings)

    def sign(
        self,
        signing_id: str,
        slot_id: str,
        position: str = "single",
        *,
        readings: Mapping[tuple, fit.Reading] | None = None,
    ) -> dict:
        """Sign a slot's admissible sizes, grain and colour at one cut.

        Refuses when the opening/laps are missing or contradictory, when a
        grained slot has no grain-axis reading, or when no size is jointly
        feasible.  The signed slip is immutable and cut-pinned.
        """

        readings = readings if readings is not None else self.reading_set()
        req = self.requirement_for(slot_id, position, readings)
        errors = req.requirement_errors()
        if errors:
            raise fit.FitError("; ".join(errors))
        slot = self.get_slot(slot_id)
        if slot.grained and req.grain is None:
            raise fit.FitError(f"slot {slot_id} is grained but no grain axis was measured")
        sizes = req.maximal_sizes()
        if not sizes:
            raise fit.FitError(
                f"no admissible pane size for slot {slot_id}: the lap ranges give "
                "no jointly feasible size"
            )
        cut = self.current_glass_cut()
        payload = {
            "signing_id": signing_id,
            "slot_id": slot_id,
            "position": position,
            "color": slot.color,
            "grain": req.grain,
            "sizes": [
                {"width": s["width"], "height": s["height"], "laps": s["laps"]}
                for s in sizes
            ],
            "opening": list(req.opening) if req.opening else None,
            "warnings": req.warnings,
            "cut": cut,
        }
        with self._lock(), _Transaction(self.conn):
            existing = self.conn.execute(
                "SELECT payload FROM glass_signings WHERE signing_id=?", (signing_id,)
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload"]) != payload:
                    raise GlassError(
                        f"signing {signing_id!r} already exists with different content"
                    )
                return json.loads(existing["payload"])
            self.conn.execute(
                "INSERT INTO glass_signings"
                "(signing_id, slot_id, position, color, grain, sizes, "
                "cut_catalog, cut_fact, cut_watermark, created_at, payload)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signing_id, slot_id, position, slot.color, req.grain,
                    _json_dump([[s["width"], s["height"]] for s in sizes]),
                    cut["catalog_revision"], "F0", cut["glass_watermark"],
                    _utc_now(), _json_dump(payload),
                ),
            )
        return payload

    def get_signing(self, signing_id: str) -> dict:
        row = self.conn.execute(
            "SELECT payload FROM glass_signings WHERE signing_id=?", (signing_id,)
        ).fetchone()
        if row is None:
            raise GlassError(f"unknown signing {signing_id!r}")
        return json.loads(row["payload"])

    def signings(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload FROM glass_signings ORDER BY signing_id"
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def explain_size(self, signing_id: str, width: int, height: int) -> dict:
        payload = self.get_signing(signing_id)
        req = self.requirement_for(payload["slot_id"], payload["position"])
        result = req.explain(width, height)
        result["signing_id"] = signing_id
        return result

    # ---------------------------------------------------------------- stock
    def register_stock(
        self, batch_id: str, pieces: Sequence[Mapping[str, Any]], remaining_cuts: int
    ) -> dict:
        built = [_piece_from_dict(p) for p in pieces]
        with self._lock(), _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO glass_stock(batch_id, created_at, remaining_cuts, pieces)"
                " VALUES(?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET "
                "remaining_cuts=excluded.remaining_cuts, pieces=excluded.pieces",
                (batch_id, _utc_now(), remaining_cuts,
                 _json_dump([p.as_dict() for p in built])),
            )
        return {"batch_id": batch_id,
                "pieces": [p.as_dict() for p in built],
                "remaining_cuts": remaining_cuts}

    def get_stock(self, batch_id: str) -> tuple[list[Piece], int]:
        row = self.conn.execute(
            "SELECT pieces, remaining_cuts FROM glass_stock WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise GlassError(f"unknown stock batch {batch_id!r}")
        return [_piece_from_dict(p) for p in json.loads(row["pieces"])], int(
            row["remaining_cuts"]
        )

    def pane_specs(self, signing_ids: Sequence[str]) -> list[PaneSpec]:
        """One demand per *slot*, using the union of its signings' sizes.

        A groove delivers exactly one pane, so two signings for the same slot
        (e.g. a re-signed slip) must not demand two.  Their legal sizes are
        unioned and the planner still picks a single size for the slot.
        """

        by_slot: dict[tuple[str, str], dict] = {}
        for signing_id in signing_ids:
            payload = self.get_signing(signing_id)
            key = (payload["slot_id"], payload["position"])
            entry = by_slot.setdefault(
                key,
                {"signings": [], "sizes": set(),
                 "slot": payload["slot_id"], "position": payload["position"],
                 "grain": payload["grain"]},
            )
            entry["signings"].append(signing_id)
            for s in payload["sizes"]:
                entry["sizes"].add((s["width"], s["height"]))
            if payload["grain"] is not None:
                entry["grain"] = payload["grain"]

        specs = []
        for (slot_id, position), entry in sorted(by_slot.items()):
            slot = self.get_slot(slot_id)
            req = self.requirement_for(slot_id, position)
            specs.append(
                PaneSpec(
                    demand_id=entry["signings"][0],
                    slot=slot_id,
                    color=slot.color,
                    grain=entry["grain"] if slot.grained else None,
                    sizes=sorted(entry["sizes"]),
                    lap_for=req.feasible_laps,
                )
            )
        return specs

    def plan(
        self,
        plan_id: str,
        batch_id: str,
        signing_ids: Sequence[str],
        *,
        minimise_cuts: bool = False,
    ) -> dict:
        pieces, remaining = self.get_stock(batch_id)
        ledger = Ledger(pieces, remaining)
        specs = self.pane_specs(signing_ids)
        result: Plan = plan_cuts(specs, ledger, minimise_cuts=minimise_cuts)
        payload = result.as_dict()
        payload["format"] = "glass-cut-plan/1"
        payload["batch_id"] = batch_id
        payload["signings"] = list(signing_ids)
        payload["handoff"] = {
            "remaining_cuts_given": remaining,
            "physical_fracture_check": check_cuts_within_pieces(
                result.placements, pieces
            ),
        }
        with self._lock(), _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO glass_plans"
                "(plan_id, batch_id, signing_ids, cuts_used, payload, created_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET "
                "payload=excluded.payload, cuts_used=excluded.cuts_used",
                (plan_id, batch_id, _json_dump(list(signing_ids)),
                 len(result.cuts), _json_dump(payload), _utc_now()),
            )
        return payload

    def get_plan(self, plan_id: str) -> dict:
        row = self.conn.execute(
            "SELECT payload FROM glass_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise GlassError(f"unknown plan {plan_id!r}")
        return json.loads(row["payload"])

    def resume(
        self,
        plan_id: str,
        batch_id: str,
        *,
        delivered: Sequence[str] = (),
        minimise_cuts: bool = False,
    ) -> dict:
        """Continue a plan from physical remnants and the remaining cut budget.

        ``delivered`` lists the signing ids already cut and handed over.  The
        stock batch must describe the bench *as it now is* (fracture lines
        included), so the remaining slots are planned only from pieces that
        actually exist; already-separated halves are never re-joined.
        """

        original = self.get_plan(plan_id)
        remaining_slots = [
            sid for sid in original["signings"] if sid not in set(delivered)
        ]
        pieces, remaining = self.get_stock(batch_id)
        payload = {
            "format": "glass-resume/1",
            "plan_id": plan_id,
            "resumed_from": list(original["signings"]),
            "delivered": list(delivered),
            "remaining_signings": remaining_slots,
            "handoff_pieces": [p.as_dict() for p in pieces],
            "remaining_cuts_given": remaining,
        }
        if not remaining_slots:
            payload["placements"] = []
            payload["cuts"] = []
            payload["cuts_used"] = 0
            payload["remaining_cuts"] = remaining
            payload["note"] = "every slot already delivered"
            return payload
        ledger = Ledger(pieces, remaining)
        specs = self.pane_specs(remaining_slots)
        result = plan_cuts(specs, ledger, minimise_cuts=minimise_cuts)
        payload.update(result.as_dict())
        payload["fracture_check"] = check_cuts_within_pieces(
            result.placements, pieces
        )
        return payload

    # --------------------------------------------------------------- helper
    def _lock(self):
        return self.store._write_lock


# ------------------------------------------------------------------ helpers


def check_cuts_within_pieces(placements, pieces: Sequence[Piece]) -> list[str]:
    problems = []
    for placement in placements:
        crop = placement.crop
        if not any(p.clear(*crop) for p in pieces):
            problems.append(
                f"{placement.demand_id}: crop {list(crop)} spans no single "
                "handed-over piece"
            )
    return problems


def _piece_from_dict(raw: Mapping[str, Any]) -> Piece:
    return Piece(
        raw["id"],
        raw.get("source", raw["id"]),
        int(raw.get("x", 0)),
        int(raw.get("y", 0)),
        int(raw["width"]),
        int(raw["height"]),
        raw["grain"],
        raw["color"],
        tuple(
            Defect(int(d["x"]), int(d["y"]), int(d["width"]), int(d["height"]))
            for d in raw.get("defects", [])
        ),
    )


def _check_no_same_round_conflict(readings: Sequence[fit.Reading]) -> None:
    seen: dict[tuple, tuple] = {}
    for r in readings:
        if r.key in seen and seen[r.key] != r.payload:
            raise fit.FitError(
                f"conflicting readings at round {r.revision} for {r.item} on "
                f"{r.slot}/{r.install_position}"
                + (f" {r.target}" if r.target else "")
                + "; re-take the measurement"
            )
        seen[r.key] = r.payload


def _select_from_normalized(raw: Sequence[Mapping[str, Any]]) -> dict[tuple, fit.Reading]:
    """Highest round per key over already-normalised readings."""

    chosen: dict[tuple, fit.Reading] = {}
    for r in raw:
        reading = fit.Reading(
            r["slot"], r["install_position"], r["item"], r["revision"],
            r["target"], tuple(r["payload"]),
        )
        current = chosen.get(reading.key)
        if current is None or reading.revision > current.revision:
            chosen[reading.key] = reading
        elif reading.revision == current.revision and reading.payload != current.payload:
            raise fit.FitError(
                f"conflicting readings at round {reading.revision} for "
                f"{reading.item} on {reading.slot}/{reading.install_position}"
                + (f" {reading.target}" if reading.target else "")
                + "; re-take the measurement"
            )
    return chosen


def readings_for_position(
    readings: Mapping[tuple, fit.Reading], slot: str, position: str
) -> dict[tuple, fit.Reading]:
    return {k: v for k, v in readings.items() if k[0] == slot and k[1] == position}

"""Curtain service: countersigning, seam queries, planning, sealed trucks.

The measurement room countersigns a snapshot of every active record
(stations, registered references, edge readings) into an immutable
revision.  Seam queries and the sewing planner always run against one such
revision, and every sealed loading list names the revision its arrangements
were proved against — so a later re-approval produces a *new* list while
the old one stays exactly as signed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Iterable, Sequence

from reagent import bottles as reagent_bottles
from reagent.errors import Conflict, NotFoundError, ValidationError
from reagent.rules import advance_day, require_int, require_text
from reagent.schema import transaction

from . import reconcile
from .arrange import arrangement_spares, plan_arrangement
from .errors import CurtainError, InfeasibleRecords, NoArrangement


class CurtainService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------
    # setup: stations, edges, panels, shows
    # ------------------------------------------------------------------
    def setup(self, document: dict, day: int) -> dict:
        """Load stations, edge readings, panels and shows (empty curtain DB)."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            if self.conn.execute(
                "SELECT 1 FROM curtain_stations LIMIT 1"
            ).fetchone():
                raise Conflict("curtain store is already set up")

            stations = _validate_stations(document.get("stations"))
            for station in stations:
                self.conn.execute(
                    "INSERT INTO curtain_stations(station_id, lo, hi) "
                    "VALUES(?, ?, ?)",
                    (station["station_id"], station["lo"], station["hi"]),
                )
            known_stations = {s["station_id"] for s in stations}

            edges = _validate_edges(document.get("edges"), known_stations)
            for edge_id, readings in edges.items():
                self.conn.execute(
                    "INSERT INTO curtain_edges(edge_id) VALUES(?)", (edge_id,)
                )
                for reading in readings:
                    self.conn.execute(
                        "INSERT INTO curtain_edge_readings"
                        "(edge_id, station_id, reading, error) VALUES(?, ?, ?, ?)",
                        (
                            edge_id,
                            reading["station_id"],
                            reading["reading"],
                            reading["error"],
                        ),
                    )

            panels = _validate_panels(document.get("panels"), set(edges))
            for panel in panels:
                self.conn.execute(
                    "INSERT INTO curtain_panels"
                    "(panel_id, kind, left_edge, right_edge, reversible) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        panel["panel_id"],
                        panel["kind"],
                        panel["left_edge"],
                        panel["right_edge"],
                        1 if panel["reversible"] else 0,
                    ),
                )
            shows = _validate_shows(document.get("shows"), panels)
            for show in shows:
                self.conn.execute(
                    "INSERT INTO curtain_shows(show_id, tolerance) VALUES(?, ?)",
                    (show["show_id"], show["tolerance"]),
                )
                for position, panel_id in enumerate(show["positions"], start=1):
                    self.conn.execute(
                        "INSERT INTO curtain_show_positions"
                        "(show_id, position, panel_id) VALUES(?, ?, ?)",
                        (show["show_id"], position, panel_id),
                    )
                for permission in show["spares"]:
                    for position in permission["positions"]:
                        self.conn.execute(
                            "INSERT INTO curtain_spare_permissions"
                            "(show_id, position, panel_id) VALUES(?, ?, ?)",
                            (show["show_id"], position, permission["panel_id"]),
                        )
            return {
                "stations": len(stations),
                "edges": len(edges),
                "panels": len(panels),
                "shows": len(shows),
            }

    # ------------------------------------------------------------------
    # references (backed by consumed portions of the reagent store)
    # ------------------------------------------------------------------
    def register_reference(
        self,
        ref_id: str,
        bottle_id: str,
        station_id: str,
        reading: int,
        error: int,
        day: int,
    ) -> dict:
        """Register one reference reading of a bottle portion.

        Only accepted while the bottle has more consumed portions than
        already-registered references: every successfully drawn portion
        backs at most one reference, and a portion whose checkout failed
        backs none.
        """
        with transaction(self.conn):
            advance_day(self.conn, day)
            ref_id = require_text(ref_id, "ref_id")
            reagent_bottles.fetch_bottle(self.conn, bottle_id)
            self._require_station(station_id)
            reading = require_int(reading, "reading")
            error = require_int(error, "error", minimum=0)
            if self.conn.execute(
                "SELECT 1 FROM curtain_references WHERE ref_id = ?", (ref_id,)
            ).fetchone():
                raise Conflict(f"reference {ref_id!r} already exists")
            self._require_portion_backing(bottle_id)
            self.conn.execute(
                "INSERT INTO curtain_references"
                "(ref_id, bottle_id, station_id, reading, error, status, "
                "registered_day) VALUES(?, ?, ?, ?, ?, 'registered', ?)",
                (ref_id, bottle_id, station_id, reading, error, day),
            )
            return self.get_reference(ref_id)

    def revoke_reference(self, ref_id: str, day: int) -> dict:
        """Withdraw a reference; existing revisions keep their snapshot."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            row = self._fetch_reference(ref_id)
            if row["status"] != "registered":
                raise Conflict(f"reference {ref_id!r} is not registered")
            self.conn.execute(
                "UPDATE curtain_references SET status = 'revoked', "
                "revoked_day = ? WHERE ref_id = ?",
                (day, ref_id),
            )
            return self.get_reference(ref_id)

    def reapprove_reference(self, ref_id: str, day: int) -> dict:
        """Re-approve a revoked reference (its portion backing is rechecked)."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            row = self._fetch_reference(ref_id)
            if row["status"] != "revoked":
                raise Conflict(f"reference {ref_id!r} is not revoked")
            self._require_portion_backing(row["bottle_id"])
            self.conn.execute(
                "UPDATE curtain_references SET status = 'registered', "
                "revoked_day = NULL WHERE ref_id = ?",
                (ref_id,),
            )
            return self.get_reference(ref_id)

    def get_reference(self, ref_id: str) -> dict:
        row = self._fetch_reference(ref_id)
        return {
            "ref_id": row["ref_id"],
            "bottle_id": row["bottle_id"],
            "station_id": row["station_id"],
            "reading": row["reading"],
            "error": row["error"],
            "status": row["status"],
            "registered_day": row["registered_day"],
            "revoked_day": row["revoked_day"],
        }

    def _fetch_reference(self, ref_id: str) -> sqlite3.Row:
        require_text(ref_id, "ref_id")
        row = self.conn.execute(
            "SELECT * FROM curtain_references WHERE ref_id = ?", (ref_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown reference {ref_id!r}")
        return row

    def _require_station(self, station_id: str) -> None:
        require_text(station_id, "station_id")
        if not self.conn.execute(
            "SELECT 1 FROM curtain_stations WHERE station_id = ?", (station_id,)
        ).fetchone():
            raise NotFoundError(f"unknown station {station_id!r}")

    def _require_portion_backing(self, bottle_id: str) -> None:
        consumed = self.conn.execute(
            "SELECT COALESCE(SUM(portions), 0) AS n FROM reservations "
            "WHERE bottle_id = ? AND status = 'consumed'",
            (bottle_id,),
        ).fetchone()["n"]
        registered = self.conn.execute(
            "SELECT COUNT(*) AS n FROM curtain_references "
            "WHERE bottle_id = ? AND status = 'registered'",
            (bottle_id,),
        ).fetchone()["n"]
        if int(consumed) < int(registered) + 1:
            raise Conflict(
                f"bottle {bottle_id!r}: {int(consumed)} consumed portion(s) "
                f"cannot back {int(registered) + 1} references"
            )

    # ------------------------------------------------------------------
    # countersigning
    # ------------------------------------------------------------------
    def countersign(self, day: int) -> dict:
        """Snapshot and countersign every active record into a new revision.

        Always answers even when the records conflict: the revision then
        stores the conflicting core (a set of original readings that cannot
        hold simultaneously) instead of a witness.
        """
        with transaction(self.conn):
            advance_day(self.conn, day)
            records = self._active_records()
            revision_id = "rev-" + hashlib.sha256(
                json.dumps(records, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12]
            existing = self.conn.execute(
                "SELECT * FROM curtain_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if existing is not None:
                return self._revision_view(existing, existing=True)

            constraints = reconcile.build_constraints(records)
            core = reconcile.find_conflict_core(constraints)
            if core is not None:
                self.conn.execute(
                    "INSERT INTO curtain_revisions"
                    "(revision_id, created_day, records_json, records_hash, "
                    "feasible, witness_json, core_json) "
                    "VALUES(?, ?, ?, ?, 0, NULL, ?)",
                    (
                        revision_id,
                        day,
                        json.dumps(records, sort_keys=True),
                        revision_id[4:],
                        json.dumps(core),
                    ),
                )
            else:
                reconciler = reconcile.Reconciler(records)
                witness = reconciler.witness()
                violations = reconcile.verify_witness(records, witness)
                if violations:
                    raise CurtainError(
                        f"internal witness violates records: {violations}"
                    )
                self.conn.execute(
                    "INSERT INTO curtain_revisions"
                    "(revision_id, created_day, records_json, records_hash, "
                    "feasible, witness_json, core_json) "
                    "VALUES(?, ?, ?, ?, 1, ?, NULL)",
                    (
                        revision_id,
                        day,
                        json.dumps(records, sort_keys=True),
                        revision_id[4:],
                        json.dumps(witness, sort_keys=True),
                    ),
                )
            row = self.conn.execute(
                "SELECT * FROM curtain_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            return self._revision_view(row, existing=False)

    def get_revision(self, revision_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM curtain_revisions WHERE revision_id = ?",
            (require_text(revision_id, "revision_id"),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown revision {revision_id!r}")
        return self._revision_view(row, existing=True)

    def _revision_view(self, row: sqlite3.Row, existing: bool) -> dict:
        return {
            "revision_id": row["revision_id"],
            "created_day": row["created_day"],
            "feasible": bool(row["feasible"]),
            "witness": json.loads(row["witness_json"])
            if row["witness_json"]
            else None,
            "core": json.loads(row["core_json"]) if row["core_json"] else None,
            "records": json.loads(row["records_json"]),
            "existing": existing,
        }

    def _active_records(self) -> dict:
        stations = [
            {"station_id": r["station_id"], "lo": r["lo"], "hi": r["hi"]}
            for r in self.conn.execute(
                "SELECT * FROM curtain_stations ORDER BY station_id"
            )
        ]
        references = [
            {
                "ref_id": r["ref_id"],
                "bottle_id": r["bottle_id"],
                "station_id": r["station_id"],
                "reading": r["reading"],
                "error": r["error"],
            }
            for r in self.conn.execute(
                "SELECT * FROM curtain_references WHERE status = 'registered' "
                "ORDER BY ref_id"
            )
        ]
        edge_readings = [
            {
                "reading_id": f"{r['edge_id']}@{r['station_id']}",
                "edge_id": r["edge_id"],
                "station_id": r["station_id"],
                "reading": r["reading"],
                "error": r["error"],
            }
            for r in self.conn.execute(
                "SELECT * FROM curtain_edge_readings "
                "ORDER BY edge_id, station_id"
            )
        ]
        unread = [
            r["edge_id"]
            for r in self.conn.execute(
                "SELECT e.edge_id FROM curtain_edges e "
                "WHERE NOT EXISTS (SELECT 1 FROM curtain_edge_readings er "
                "                  WHERE er.edge_id = e.edge_id) "
                "ORDER BY e.edge_id"
            )
        ]
        if unread:
            raise ValidationError(
                f"edge(s) without any reading cannot be countersigned: "
                f"{', '.join(unread)}"
            )
        return {
            "stations": stations,
            "references": references,
            "edge_readings": edge_readings,
        }

    def _reconciler(self, revision_id: str) -> reconcile.Reconciler:
        revision = self.get_revision(revision_id)
        if not revision["feasible"]:
            raise InfeasibleRecords(
                f"revision {revision_id} is conflicting",
                core=revision["core"],
            )
        return reconcile.Reconciler(revision["records"])

    # ------------------------------------------------------------------
    # seam queries (independent of any sewing order)
    # ------------------------------------------------------------------
    def seam_query(self, revision_id: str, edge_a: str, edge_b: str) -> dict:
        """Signed min/max achievable difference ``t_a - t_b`` plus witnesses."""
        require_text(edge_a, "edge_a")
        require_text(edge_b, "edge_b")
        revision = self.get_revision(revision_id)
        reconciler = self._reconciler(revision_id)
        known = {r["edge_id"] for r in revision["records"]["edge_readings"]}
        for edge in (edge_a, edge_b):
            if edge not in known:
                raise NotFoundError(
                    f"edge {edge!r} has no reading in revision {revision_id}"
                )
        result = reconciler.seam_witnesses(edge_a, edge_b)
        for name in ("witness_min", "witness_max"):
            violations = reconcile.verify_witness(
                revision["records"], result[name]
            )
            if violations:
                raise CurtainError(
                    f"internal witness violates records: {violations}"
                )
        return {
            "revision_id": revision_id,
            "edge_a": edge_a,
            "edge_b": edge_b,
            **result,
        }

    # ------------------------------------------------------------------
    # sewing planner
    # ------------------------------------------------------------------
    def plan(
        self,
        show_id: str,
        revision_id: str,
        available: Iterable[str] | None = None,
    ) -> dict:
        """Minimum-swap arrangement for one show under one revision."""
        reconciler = self._reconciler(revision_id)
        old_panels, spares, tolerance = self._show_data(show_id)
        available_set = None if available is None else set(available)

        range_cache: dict[tuple, tuple] = {}

        def seam_range(edge_a: str, edge_b: str) -> tuple:
            key = (edge_a, edge_b)
            if key not in range_cache:
                range_cache[key] = reconciler.difference_range(edge_a, edge_b)
            return range_cache[key]

        arrangement = plan_arrangement(
            old_panels,
            spares,
            seam_range,
            tolerance,
            available=available_set,
        )
        placements = arrangement["placements"]
        seams = []
        for left, right in zip(placements, placements[1:]):
            lo, hi = seam_range(left["right_edge"], right["left_edge"])
            seams.append(
                {
                    "position": left["position"],
                    "right_edge": left["right_edge"],
                    "left_edge": right["left_edge"],
                    "min": lo,
                    "max": hi,
                }
            )
        return {
            "show_id": show_id,
            "revision_id": revision_id,
            "tolerance": tolerance,
            "swaps": arrangement["swaps"],
            "placements": placements,
            "seams": seams,
        }

    def _show_data(self, show_id: str) -> tuple[list[dict], list[dict], int]:
        require_text(show_id, "show_id")
        show = self.conn.execute(
            "SELECT * FROM curtain_shows WHERE show_id = ?", (show_id,)
        ).fetchone()
        if show is None:
            raise NotFoundError(f"unknown show {show_id!r}")
        old_panels = [
            {
                "panel_id": r["panel_id"],
                "left_edge": r["left_edge"],
                "right_edge": r["right_edge"],
            }
            for r in self.conn.execute(
                "SELECT p.panel_id, p.left_edge, p.right_edge "
                "FROM curtain_show_positions sp "
                "JOIN curtain_panels p ON p.panel_id = sp.panel_id "
                "WHERE sp.show_id = ? ORDER BY sp.position",
                (show_id,),
            )
        ]
        spares = [
            {
                "panel_id": r["panel_id"],
                "left_edge": r["left_edge"],
                "right_edge": r["right_edge"],
                "reversible": bool(r["reversible"]),
                "positions": [
                    row["position"]
                    for row in self.conn.execute(
                        "SELECT position FROM curtain_spare_permissions "
                        "WHERE show_id = ? AND panel_id = ? ORDER BY position",
                        (show_id, r["panel_id"]),
                    )
                ],
            }
            for r in self.conn.execute(
                "SELECT DISTINCT p.panel_id, p.left_edge, p.right_edge, "
                "p.reversible FROM curtain_spare_permissions perm "
                "JOIN curtain_panels p ON p.panel_id = perm.panel_id "
                "WHERE perm.show_id = ? ORDER BY p.panel_id",
                (show_id,),
            )
        ]
        return old_panels, spares, int(show["tolerance"])

    # ------------------------------------------------------------------
    # loading lists (sealed before departure, immutable afterwards)
    # ------------------------------------------------------------------
    def seal_loading_list(
        self,
        list_id: str,
        revision_id: str,
        show_ids: Sequence[str],
        loaded: Sequence[str],
        day: int,
    ) -> dict:
        """Seal one physical loading list for a truck.

        Plans the primary arrangement per show against exactly the loaded
        spares, plus one contingency per possibly missing spare.  Every
        sealed arrangement is checked to name only spares that exist and
        are actually loaded — the witness must match the physical load.
        """
        with transaction(self.conn):
            advance_day(self.conn, day)
            list_id = require_text(list_id, "list_id")
            self._reconciler(revision_id)  # raises if conflicting revision
            if self.conn.execute(
                "SELECT 1 FROM curtain_loading_lists WHERE list_id = ?",
                (list_id,),
            ).fetchone():
                raise Conflict(f"loading list {list_id!r} already exists")
            if not show_ids:
                raise ValidationError("a loading list needs at least one show")
            loaded_ids = [require_text(p, "loaded spare") for p in loaded]
            if len(set(loaded_ids)) != len(loaded_ids):
                raise ValidationError("loaded spares contain duplicates")
            locked = self._locked_panels()
            for panel_id in loaded_ids:
                row = self.conn.execute(
                    "SELECT kind FROM curtain_panels WHERE panel_id = ?",
                    (panel_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"unknown spare panel {panel_id!r}")
                if row["kind"] != "spare":
                    raise ValidationError(
                        f"panel {panel_id!r} is not a spare"
                    )
                if panel_id in locked:
                    raise Conflict(
                        f"spare {panel_id!r} is locked to show "
                        f"{locked[panel_id]!r} and cannot be loaded"
                    )

            shows_doc: dict[str, dict] = {}
            for show_id in show_ids:
                primary = self.plan(show_id, revision_id, available=loaded_ids)
                used = set(arrangement_spares(primary))
                contingencies: dict[str, dict | None] = {}
                for spare_id in sorted(loaded_ids):
                    if spare_id not in used:
                        contingencies[spare_id] = primary
                    else:
                        try:
                            contingencies[spare_id] = self.plan(
                                show_id,
                                revision_id,
                                available=set(loaded_ids) - {spare_id},
                            )
                        except NoArrangement:
                            contingencies[spare_id] = None
                shows_doc[show_id] = {
                    "primary": primary,
                    "contingencies": contingencies,
                }
            self.conn.execute(
                "INSERT INTO curtain_loading_lists"
                "(list_id, revision_id, sealed_day, loaded_json, shows_json, "
                "status) VALUES(?, ?, ?, ?, ?, 'sealed')",
                (
                    list_id,
                    revision_id,
                    day,
                    json.dumps(sorted(loaded_ids)),
                    json.dumps(shows_doc, sort_keys=True),
                ),
            )
            return self.get_list(list_id)

    def get_list(self, list_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM curtain_loading_lists WHERE list_id = ?",
            (require_text(list_id, "list_id"),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown loading list {list_id!r}")
        return {
            "list_id": row["list_id"],
            "revision_id": row["revision_id"],
            "sealed_day": row["sealed_day"],
            "loaded": json.loads(row["loaded_json"]),
            "shows": json.loads(row["shows_json"]),
            "status": row["status"],
            "handover": json.loads(row["handover_json"])
            if row["handover_json"]
            else {},
        }

    def depart(self, list_id: str, day: int) -> dict:
        """Mark the truck as departed with this sealed list."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            doc = self.get_list(list_id)
            if doc["status"] != "sealed":
                raise Conflict(
                    f"loading list {list_id!r} is {doc['status']}, not sealed"
                )
            self.conn.execute(
                "UPDATE curtain_loading_lists SET status = 'departed' "
                "WHERE list_id = ?",
                (list_id,),
            )
            return self.get_list(list_id)

    def onsite(self, list_id: str, present: Sequence[str]) -> dict:
        """What the truck crew can hang with the spares physically present.

        Answered from the sealed list alone — no warehouse contact needed.
        """
        return onsite_options(self.get_list(list_id), present)

    def handover(
        self,
        list_id: str,
        show_id: str,
        arrangement_key: str,
        present: Sequence[str],
        day: int,
    ) -> dict:
        """Commit one sealed arrangement on site and lock its spares.

        Locked spares belong to the show; no other troupe can take them.
        """
        with transaction(self.conn):
            advance_day(self.conn, day)
            doc = self.get_list(list_id)
            if doc["status"] != "departed":
                raise Conflict(
                    f"loading list {list_id!r} is {doc['status']}; "
                    f"handover requires a departed truck"
                )
            require_text(show_id, "show_id")
            if show_id not in doc["shows"]:
                raise NotFoundError(
                    f"show {show_id!r} is not on loading list {list_id!r}"
                )
            if show_id in doc["handover"]:
                raise Conflict(
                    f"show {show_id!r} already handed over on list {list_id!r}"
                )
            arrangement = _sealed_arrangement(doc, show_id, arrangement_key)
            needed = arrangement_spares(arrangement)
            missing = sorted(set(needed) - set(present))
            if missing:
                raise Conflict(
                    f"arrangement {arrangement_key!r} needs spare(s) not "
                    f"present on the truck: {', '.join(missing)}"
                )
            locked = self._locked_panels()
            for spare_id in needed:
                if spare_id in locked:
                    raise Conflict(
                        f"spare {spare_id!r} is already locked to show "
                        f"{locked[spare_id]!r}"
                    )
            for spare_id in needed:
                self.conn.execute(
                    "INSERT INTO curtain_locks(panel_id, show_id, list_id, "
                    "locked_day) VALUES(?, ?, ?, ?)",
                    (spare_id, show_id, list_id, day),
                )
            handover = dict(doc["handover"])
            handover[show_id] = {
                "arrangement_key": arrangement_key,
                "swaps": arrangement["swaps"],
                "spares": needed,
                "day": day,
            }
            self.conn.execute(
                "UPDATE curtain_loading_lists SET handover_json = ? "
                "WHERE list_id = ?",
                (json.dumps(handover, sort_keys=True), list_id),
            )
            return {
                "list_id": list_id,
                "show_id": show_id,
                "arrangement_key": arrangement_key,
                "swaps": arrangement["swaps"],
                "locked": needed,
                "day": day,
            }

    def locks(self) -> list[dict]:
        return [
            {
                "panel_id": r["panel_id"],
                "show_id": r["show_id"],
                "list_id": r["list_id"],
                "locked_day": r["locked_day"],
            }
            for r in self.conn.execute(
                "SELECT * FROM curtain_locks ORDER BY panel_id"
            )
        ]

    def _locked_panels(self) -> dict[str, str]:
        return {
            r["panel_id"]: r["show_id"]
            for r in self.conn.execute("SELECT panel_id, show_id FROM curtain_locks")
        }


# ----------------------------------------------------------------------
# pure on-site logic (usable off the truck's sealed document alone)
# ----------------------------------------------------------------------
def _sealed_arrangement(doc: dict, show_id: str, arrangement_key: str) -> dict:
    require_text(arrangement_key, "arrangement_key")
    show_doc = doc["shows"][show_id]
    if arrangement_key == "primary":
        return show_doc["primary"]
    if arrangement_key.startswith("missing:"):
        spare_id = arrangement_key[len("missing:"):]
        arrangement = show_doc["contingencies"].get(spare_id)
        if arrangement is None:
            raise NoArrangement(
                f"no sealed arrangement for show {show_id!r} without spare "
                f"{spare_id!r}: cannot deliver"
            )
        return arrangement
    raise ValidationError(
        f"arrangement key must be 'primary' or 'missing:<spare>', got "
        f"{arrangement_key!r}"
    )


# ----------------------------------------------------------------------
# document validation
# ----------------------------------------------------------------------
def _validate_stations(raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("stations must be a non-empty list")
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError(f"station entry must be an object: {item!r}")
        station_id = require_text(item.get("station_id"), "station_id")
        if station_id in seen:
            raise ValidationError(f"duplicate station {station_id!r}")
        seen.add(station_id)
        lo = require_int(item.get("lo"), f"station {station_id} lo")
        hi = require_int(item.get("hi"), f"station {station_id} hi")
        if lo > hi:
            raise ValidationError(
                f"station {station_id}: lo {lo} must not exceed hi {hi}"
            )
        out.append({"station_id": station_id, "lo": lo, "hi": hi})
    if not any(s["lo"] == 0 and s["hi"] == 0 for s in out):
        raise ValidationError(
            "one station must be the known zero-offset station ([0, 0])"
        )
    return out


def _validate_edges(raw: object, known_stations: set[str]) -> dict[str, list[dict]]:
    if not isinstance(raw, dict) or not raw:
        raise ValidationError("edges must be a non-empty object")
    out: dict[str, list[dict]] = {}
    for edge_id, readings in raw.items():
        require_text(edge_id, "edge_id")
        if not isinstance(readings, list) or not readings:
            raise ValidationError(f"edge {edge_id} needs at least one reading")
        seen: set[str] = set()
        entries: list[dict] = []
        for item in readings:
            if not isinstance(item, dict):
                raise ValidationError(
                    f"edge {edge_id}: reading entry must be an object"
                )
            station_id = require_text(item.get("station_id"), "station_id")
            if station_id not in known_stations:
                raise ValidationError(
                    f"edge {edge_id}: unknown station {station_id!r}"
                )
            if station_id in seen:
                raise ValidationError(
                    f"edge {edge_id}: duplicate reading on station {station_id}"
                )
            seen.add(station_id)
            entries.append(
                {
                    "station_id": station_id,
                    "reading": require_int(item.get("reading"), "reading"),
                    "error": require_int(item.get("error"), "error", minimum=0),
                }
            )
        out[edge_id] = entries
    return out


def _validate_panels(raw: object, known_edges: set[str]) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("panels must be a non-empty list")
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError(f"panel entry must be an object: {item!r}")
        panel_id = require_text(item.get("panel_id"), "panel_id")
        if panel_id in seen:
            raise ValidationError(f"duplicate panel {panel_id!r}")
        seen.add(panel_id)
        kind = require_text(item.get("kind"), "kind")
        if kind not in ("old", "spare"):
            raise ValidationError(f"panel {panel_id}: kind must be old|spare")
        left_edge = require_text(item.get("left_edge"), "left_edge")
        right_edge = require_text(item.get("right_edge"), "right_edge")
        for edge in (left_edge, right_edge):
            if edge not in known_edges:
                raise ValidationError(
                    f"panel {panel_id}: unknown edge {edge!r}"
                )
        reversible = item.get("reversible", False)
        if not isinstance(reversible, bool):
            raise ValidationError(f"panel {panel_id}: reversible must be a bool")
        out.append(
            {
                "panel_id": panel_id,
                "kind": kind,
                "left_edge": left_edge,
                "right_edge": right_edge,
                "reversible": reversible,
            }
        )
    return out


def _validate_shows(raw: object, panels: list[dict]) -> list[dict]:
    # Shows are the sewing orders; the measurement room may countersign
    # before any order exists, so an empty list is allowed.
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("shows must be a list")
    if not raw:
        return []
    kinds = {panel["panel_id"]: panel["kind"] for panel in panels}
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError(f"show entry must be an object: {item!r}")
        show_id = require_text(item.get("show_id"), "show_id")
        if show_id in seen:
            raise ValidationError(f"duplicate show {show_id!r}")
        seen.add(show_id)
        tolerance = require_int(item.get("tolerance"), "tolerance", minimum=0)
        positions = item.get("positions")
        if not isinstance(positions, list) or not positions:
            raise ValidationError(f"show {show_id}: positions must be a list")
        for panel_id in positions:
            require_text(panel_id, "position panel_id")
            if kinds.get(panel_id) != "old":
                raise ValidationError(
                    f"show {show_id}: position panel {panel_id!r} is not an "
                    f"old panel"
                )
        spares_raw = item.get("spares", [])
        if not isinstance(spares_raw, list):
            raise ValidationError(f"show {show_id}: spares must be a list")
        spares: list[dict] = []
        for entry in spares_raw:
            if not isinstance(entry, dict):
                raise ValidationError(
                    f"show {show_id}: spare entry must be an object"
                )
            panel_id = require_text(entry.get("panel_id"), "spare panel_id")
            if kinds.get(panel_id) != "spare":
                raise ValidationError(
                    f"show {show_id}: permitted panel {panel_id!r} is not a "
                    f"spare"
                )
            allowed = entry.get("positions")
            if not isinstance(allowed, list) or not allowed:
                raise ValidationError(
                    f"show {show_id}: spare {panel_id} needs positions"
                )
            for position in allowed:
                require_int(position, "spare position", minimum=1)
                if position > len(positions):
                    raise ValidationError(
                        f"show {show_id}: spare {panel_id} position "
                        f"{position} exceeds {len(positions)} positions"
                    )
            spares.append({"panel_id": panel_id, "positions": sorted(set(allowed))})
        out.append(
            {
                "show_id": show_id,
                "tolerance": tolerance,
                "positions": list(positions),
                "spares": spares,
            }
        )
    return out


def onsite_options(list_doc: dict, present: Sequence[str]) -> dict:
    """Hangable sealed arrangements per pending show, given present spares.

    Pure function of the sealed list document: the truck crew can evaluate
    it on site, after loading, without any warehouse connection.  An
    arrangement is hangable iff every spare it consumes is present.
    """
    present_set = set(present)
    handed_over = list_doc.get("handover") or {}
    answer: dict[str, dict] = {}
    for show_id in sorted(list_doc["shows"]):
        if show_id in handed_over:
            continue
        show_doc = list_doc["shows"][show_id]
        candidates = [("primary", show_doc["primary"])]
        for spare_id in sorted(show_doc["contingencies"]):
            candidates.append(
                (f"missing:{spare_id}", show_doc["contingencies"][spare_id])
            )
        options = []
        seen: set[tuple] = set()
        for key, arrangement in candidates:
            if arrangement is None:
                continue
            spares = tuple(sorted(arrangement_spares(arrangement)))
            signature = (
                spares,
                tuple(
                    (p["position"], p["panel_id"], p["flipped"])
                    for p in arrangement["placements"]
                ),
            )
            if signature in seen:
                continue
            seen.add(signature)
            if set(spares) <= present_set:
                options.append(
                    {"key": key, "swaps": arrangement["swaps"], "spares": list(spares)}
                )
        options.sort(key=lambda option: (option["swaps"], option["key"]))
        answer[show_id] = {
            "can_deliver": bool(options),
            "options": options,
        }
    return answer

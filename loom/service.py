"""Loom service: physical shuttles, the two-phase 装梭 business and weave jobs.

Thread never appears out of thin air. A shuttle is loaded through the existing
reagent run machinery:

1. ``reserve_load`` creates a real run (``create_run`` -> active
   reservations, the original 预留). Nothing is mounted yet, so a failed
   reservation can never put thread on a shuttle.
2. ``confirm_load`` calls the original ``start_run`` (实领). Only when that
   run reaches its stored ``started`` terminal receipt do portions enter the
   shuttle ledger and the shuttle dock on its factory side. If the start
   fails (e.g. validity expiry), the run releases its reservations and the
   load is recorded as failed; the shuttle stays unmounted.

Every later fact -- unload, carry (搬, one ticket), pass (过梭: one portion
consumed, head and shuttle cross sides) and cross-loom handoff -- is one
append-only row in ``shuttle_events`` with a unique receipt id. Replaying a
receipt returns the stored row instead of moving anything twice. A shuttle's
remaining portions are the sum of ledger deltas, never a stored counter.
"""

from __future__ import annotations

import json
import sqlite3

from reagent import bottles
from reagent.errors import ReagentError
from reagent.runs import RELEASED as RUN_RELEASED, STARTED as RUN_STARTED, RunService

from .errors import LoomConflict, LoomNotFound, LoomValidationError
from .planner import (
    LEFT,
    RIGHT,
    ShuttleSnapshot,
    opposite,
    plan_tape,
    validate_pattern,
)
from reagent.rules import advance_day, require_int, require_text
from reagent.schema import transaction

REGISTERED = "registered"
LOADED = "loaded"
EMPTY = "empty"
UNLOADED = "unloaded"

PLANNED = "planned"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
VOID = "void"
ACTIVE_JOB_STATES = (PLANNED, RUNNING)
TERMINAL_JOB_STATES = (COMPLETED, FAILED, VOID)


class LoomService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.runs = RunService(conn)

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register_loom(self, loom_id: str, head_side: str, day: int) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            loom_id = require_text(loom_id, "loom_id")
            if head_side not in (LEFT, RIGHT):
                raise LoomValidationError("head_side must be 'left' or 'right'")
            if self._fetch_optional("looms", "loom_id", loom_id) is not None:
                raise LoomConflict(f"loom {loom_id!r} already exists")
            self.conn.execute(
                "INSERT INTO looms(loom_id, head_side, created_day) "
                "VALUES(?, ?, ?)",
                (loom_id, head_side, day),
            )
            return self.get_loom(loom_id)

    def register_shuttle(
        self,
        shuttle_id: str,
        core_id: str,
        color: str,
        factory_side: str,
        day: int,
    ) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            shuttle_id = require_text(shuttle_id, "shuttle_id")
            core_id = require_text(core_id, "core_id")
            color = require_text(color, "color")
            if factory_side not in (LEFT, RIGHT):
                raise LoomValidationError("factory_side must be 'left' or 'right'")
            if self._fetch_optional("shuttles", "shuttle_id", shuttle_id) is not None:
                raise LoomConflict(f"shuttle {shuttle_id!r} already exists")
            if self.conn.execute(
                "SELECT 1 FROM shuttles WHERE core_id = ?", (core_id,)
            ).fetchone():
                raise LoomConflict(f"core {core_id!r} is already bound to a shuttle")
            self.conn.execute(
                "INSERT INTO shuttles(shuttle_id, core_id, color, factory_side, "
                "loom_id, side, state, created_day) VALUES(?, ?, ?, ?, NULL, "
                "NULL, 'registered', ?)",
                (shuttle_id, core_id, color, factory_side, day),
            )
            return self.get_shuttle(shuttle_id)

    # ------------------------------------------------------------------
    # 装梭: phase 1 -- reserve through the original run service (预留)
    # ------------------------------------------------------------------
    def reserve_load(
        self,
        load_id: str,
        shuttle_id: str,
        loom_id: str,
        bottle_id: str,
        portions: int,
        day: int,
    ) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            load_id = require_text(load_id, "load_id")
            portions = require_int(portions, "portions", minimum=1)
            existing = self._fetch_optional("loom_loads", "load_id", load_id)
            if existing is not None:
                # Replaying the reservation just returns the stored request.
                return self._load_view(existing, replayed=True)

            shuttle = self._fetch_shuttle(shuttle_id)
            if shuttle["state"] not in (REGISTERED, UNLOADED):
                raise LoomConflict(
                    f"shuttle {shuttle_id} cannot be loaded from state "
                    f"{shuttle['state']!r}"
                )
            loom = self._fetch_loom(loom_id)
            bottle = bottles.fetch_bottle(self.conn, bottle_id)
            if bottle.kind != shuttle["color"]:
                raise LoomValidationError(
                    f"shuttle {shuttle_id} holds color {shuttle['color']!r} but "
                    f"bottle {bottle_id} is kind {bottle.kind!r}"
                )

            method_id = self._ensure_load_method(shuttle["color"], portions, day)
            # The run id is the load id: inventory reservations and the load
            # request share one identity, and run terminal receipts give us
            # idempotent replay for free.
            try:
                self.runs.create_run(
                    load_id,
                    method_id,
                    1,
                    day,
                    [{"bottle_id": bottle_id, "portions": portions}],
                )
            except ReagentError:
                # Short lead or invalid bottle: nothing is mounted, no half
                # load exists. Propagate after the (rolled-back) transaction.
                raise

            self.conn.execute(
                "INSERT INTO loom_loads(load_id, run_id, shuttle_id, loom_id, "
                "bottle_id, color, portions, reserve_day, state) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'reserved')",
                (
                    load_id, load_id, shuttle_id, loom_id, bottle_id,
                    shuttle["color"], portions, day,
                ),
            )
            return self._load_view(self._fetch_load(load_id))

    # ------------------------------------------------------------------
    # 装梭: phase 2 -- start the run (实领); only success mounts thread
    # ------------------------------------------------------------------
    def confirm_load(self, load_id: str, day: int) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            load = self._fetch_load(load_id)
            if load["state"] != "reserved":
                receipt = json.loads(load["terminal_json"])
                receipt["replayed"] = True
                return receipt

            run_receipt = self.runs.start_run(load_id, day)

            if run_receipt["state"] == RUN_RELEASED:
                # The original start failed (e.g. validity). Reservations were
                # released by the run service; no thread may enter the shuttle.
                receipt = {
                    "load_id": load_id,
                    "state": "failed",
                    "shuttle_id": load["shuttle_id"],
                    "day": day,
                    "reason": run_receipt.get("reason"),
                    "run_receipt": run_receipt,
                }
                self.conn.execute(
                    "UPDATE loom_loads SET state = 'failed', terminal_day = ?, "
                    "terminal_json = ? WHERE load_id = ?",
                    (day, json.dumps(receipt, sort_keys=True), load_id),
                )
                receipt["replayed"] = False
                return receipt

            if run_receipt["state"] != RUN_STARTED:
                raise LoomConflict(
                    f"load {load_id}: unexpected run state {run_receipt['state']!r}"
                )

            shuttle = self._fetch_shuttle(load["shuttle_id"])
            dock_side = shuttle["factory_side"]
            self.conn.execute(
                "UPDATE shuttles SET loom_id = ?, side = ?, state = 'loaded' "
                "WHERE shuttle_id = ?",
                (load["loom_id"], dock_side, shuttle["shuttle_id"]),
            )
            event_id = f"{load_id}#load"
            self._insert_event(
                event_id=event_id,
                shuttle_id=shuttle["shuttle_id"],
                event_type="load",
                day=day,
                loom_id=load["loom_id"],
                side=dock_side,
                delta=int(load["portions"]),
                detail={
                    "load_id": load_id,
                    "run_id": load["run_id"],
                    "bottle_id": load["bottle_id"],
                    "portions": int(load["portions"]),
                },
            )
            receipt = {
                "load_id": load_id,
                "state": "loaded",
                "shuttle_id": shuttle["shuttle_id"],
                "loom_id": load["loom_id"],
                "side": dock_side,
                "remaining": int(load["portions"]),
                "day": day,
                "reason": None,
                "run_receipt": run_receipt,
                "event_id": event_id,
            }
            self.conn.execute(
                "UPDATE loom_loads SET state = 'loaded', terminal_day = ?, "
                "terminal_json = ? WHERE load_id = ?",
                (day, json.dumps(receipt, sort_keys=True), load_id),
            )
            receipt["replayed"] = False
            return receipt

    # ------------------------------------------------------------------
    # physical shuttle moves (each is an idempotent receipt)
    # ------------------------------------------------------------------
    def unload_shuttle(self, receipt_id: str, shuttle_id: str, day: int) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            replay = self._replay_event(receipt_id)
            if replay is not None:
                return replay
            shuttle = self._require_mounted(shuttle_id)
            if self.remaining(shuttle_id) != 0:
                raise LoomConflict(
                    f"shuttle {shuttle_id} still has "
                    f"{self.remaining(shuttle_id)} portion(s); only an empty "
                    "shuttle can be unloaded"
                )
            self._insert_event(
                event_id=receipt_id,
                shuttle_id=shuttle_id,
                event_type="unload",
                day=day,
                loom_id=shuttle["loom_id"],
                side=shuttle["side"],
                delta=0,
                detail={"reason": "unloaded after depletion"},
            )
            self.conn.execute(
                "UPDATE shuttles SET loom_id = NULL, side = NULL, "
                "state = 'unloaded' WHERE shuttle_id = ?",
                (shuttle_id,),
            )
            return self._event_receipt(receipt_id)

    def carry_shuttle(self, receipt_id: str, shuttle_id: str, day: int) -> dict:
        """搬梭工 moves one shuttle to the opposite side alone; one ticket."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            replay = self._replay_event(receipt_id)
            if replay is not None:
                return replay
            shuttle = self._require_mounted(shuttle_id)
            self._require_thread(shuttle)
            new_side = opposite(shuttle["side"])
            self._insert_event(
                event_id=receipt_id,
                shuttle_id=shuttle_id,
                event_type="carry",
                day=day,
                loom_id=shuttle["loom_id"],
                side=new_side,
                delta=0,
                detail={"from_side": shuttle["side"], "ticket": 1},
            )
            self.conn.execute(
                "UPDATE shuttles SET side = ? WHERE shuttle_id = ?",
                (new_side, shuttle_id),
            )
            return self._event_receipt(receipt_id)

    def pass_shuttle(self, receipt_id: str, shuttle_id: str, day: int) -> dict:
        """过梭: consume one portion; shuttle and head cross to the other side."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            replay = self._replay_event(receipt_id)
            if replay is not None:
                return replay
            shuttle = self._require_mounted(shuttle_id)
            self._require_thread(shuttle)
            loom = self._fetch_loom(shuttle["loom_id"])
            if loom["head_side"] != shuttle["side"]:
                raise LoomConflict(
                    f"shuttle {shuttle_id} is on {shuttle['side']} but the head "
                    f"of loom {loom['loom_id']} is on {loom['head_side']}"
                )
            return self._do_pass(shuttle, loom, day, receipt_id, detail={})

    def handoff_shuttle(
        self, receipt_id: str, shuttle_id: str, target_loom_id: str, day: int
    ) -> dict:
        """整梭跨机交接: remaining thread travels with the shuttle; no side flip."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            replay = self._replay_event(receipt_id)
            if replay is not None:
                return replay
            shuttle = self._require_mounted(shuttle_id)
            self._require_thread(shuttle)
            target = self._fetch_loom(target_loom_id)
            if target["loom_id"] == shuttle["loom_id"]:
                raise LoomConflict("handoff target must be a different loom")
            self._insert_event(
                event_id=receipt_id,
                shuttle_id=shuttle_id,
                event_type="handoff",
                day=day,
                loom_id=target_loom_id,
                side=shuttle["side"],
                delta=0,
                detail={"from_loom": shuttle["loom_id"], "to_loom": target_loom_id},
            )
            self.conn.execute(
                "UPDATE shuttles SET loom_id = ? WHERE shuttle_id = ?",
                (target_loom_id, shuttle_id),
            )
            return self._event_receipt(receipt_id)

    # ------------------------------------------------------------------
    # weave jobs and the execution tape
    # ------------------------------------------------------------------
    def create_weave_job(
        self,
        job_id: str,
        loom_id: str,
        pattern: list[str],
        ticket_budget: int,
        day: int,
    ) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            job_id = require_text(job_id, "job_id")
            pattern = validate_pattern(pattern)
            require_int(ticket_budget, "ticket_budget", minimum=0)
            if self._fetch_optional("weave_jobs", "job_id", job_id) is not None:
                raise LoomConflict(f"weave job {job_id!r} already exists")
            self._require_no_active_job(loom_id)
            loom = self._fetch_loom(loom_id)

            plan = plan_tape(
                self.mounted_snapshots(loom_id),
                loom["head_side"],
                pattern,
                ticket_budget,
            )
            if not plan["feasible"]:
                raise LoomConflict(
                    f"weave job {job_id} cannot be accepted: {plan['reason']}"
                )

            self.conn.execute(
                "INSERT INTO weave_jobs(job_id, loom_id, pattern_json, "
                "ticket_budget, tickets_used, initial_head_side, rows_done, "
                "state, created_day) VALUES(?, ?, ?, ?, 0, ?, 0, 'planned', ?)",
                (
                    job_id, loom_id, json.dumps(pattern), ticket_budget,
                    loom["head_side"], day,
                ),
            )
            for index, action in enumerate(plan["actions"]):
                self.conn.execute(
                    "INSERT INTO weave_steps(job_id, step_index, row_index, kind, "
                    "shuttle_id, side, color, planned_event_id, detail_json) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        index,
                        action.get("row_index"),
                        action["kind"],
                        action["shuttle_id"],
                        action["side"],
                        action.get("color"),
                        f"{job_id}#evt#{index}",
                        json.dumps(action, sort_keys=True),
                    ),
                )
            return self.get_job(job_id)

    def verify_job(self, job_id: str) -> dict:
        """Re-check the whole remaining tape against the physical ledger.

        A rehearsal, a cross-loom handoff, or any porter move can invalidate a
        compiled tape; formal weaving must call this and may only proceed when
        ``ok`` is true.
        """
        job = self._fetch_job(job_id)
        if job["state"] in TERMINAL_JOB_STATES:
            return {"ok": False, "reason": f"job is {job['state']}"}
        return self._simulate_tape(job)

    def weave_next(self, job_id: str, day: int) -> dict:
        """Execute the next pending tape step; resumes after a power cut.

        Already-``done`` steps are never repeated, so a restart after shutdown
        continues from the last actually completed row without spending thread
        twice. If verification fails the job is marked ``failed`` in the same
        transaction rather than weaving half an order on stale assumptions.
        """
        with transaction(self.conn):
            advance_day(self.conn, day)
            job = self._fetch_job(job_id)
            if job["state"] in TERMINAL_JOB_STATES:
                if job["state"] == COMPLETED:
                    return {
                        "job_id": job_id,
                        "state": COMPLETED,
                        "replayed": True,
                        "product": json.loads(job["product_json"]),
                    }
                return {"job_id": job_id, "state": job["state"], "replayed": True}

            check = self._simulate_tape(job)
            if not check["ok"]:
                self.conn.execute(
                    "UPDATE weave_jobs SET state = 'failed', failure_reason = ? "
                    "WHERE job_id = ?",
                    (check["reason"], job_id),
                )
                return {"job_id": job_id, "state": FAILED, "reason": check["reason"]}

            if job["state"] == PLANNED:
                self.conn.execute(
                    "UPDATE weave_jobs SET state = 'running' WHERE job_id = ?",
                    (job_id,),
                )

            step = self.conn.execute(
                "SELECT * FROM weave_steps WHERE job_id = ? AND status = 'pending' "
                "ORDER BY step_index LIMIT 1",
                (job_id,),
            ).fetchone()
            if step is None:
                return self._complete_job(job_id, day)

            shuttle = self._fetch_shuttle(step["shuttle_id"])
            loom = self._fetch_loom(job["loom_id"])
            if step["kind"] == "carry":
                self.carry_shuttle(step["planned_event_id"], shuttle["shuttle_id"], day)
                self.conn.execute(
                    "UPDATE weave_jobs SET tickets_used = tickets_used + 1 "
                    "WHERE job_id = ?",
                    (job_id,),
                )
            else:
                self._do_pass(
                    shuttle,
                    loom,
                    day,
                    step["planned_event_id"],
                    detail={"job_id": job_id, "row_index": step["row_index"]},
                )
                self.conn.execute(
                    "UPDATE weave_jobs SET rows_done = rows_done + 1 "
                    "WHERE job_id = ?",
                    (job_id,),
                )
            self.conn.execute(
                "UPDATE weave_steps SET status = 'done', event_id = ? "
                "WHERE job_id = ? AND step_index = ?",
                (step["planned_event_id"], job_id, step["step_index"]),
            )

            job = self._fetch_job(job_id)
            remaining_steps = self.conn.execute(
                "SELECT COUNT(*) AS n FROM weave_steps WHERE job_id = ? "
                "AND status = 'pending'",
                (job_id,),
            ).fetchone()["n"]
            if remaining_steps == 0:
                return self._complete_job(job_id, day)
            return self.get_job(job_id)

    def weave_all(self, job_id: str, day: int) -> dict:
        """Drive a tape to completion; each step is its own committed fact."""
        last = None
        while True:
            last = self.weave_next(job_id, day)
            if last.get("state") in TERMINAL_JOB_STATES:
                return last

    def void_job(self, job_id: str, reason: str, day: int) -> dict:
        """Void a (partly woven) tape so the remaining suffix can be replanned."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            require_text(reason, "reason")
            job = self._fetch_job(job_id)
            if job["state"] in TERMINAL_JOB_STATES:
                raise LoomConflict(f"job {job_id} is already {job['state']}")
            self.conn.execute(
                "UPDATE weave_steps SET status = 'skipped' WHERE job_id = ? "
                "AND status = 'pending'",
                (job_id,),
            )
            self.conn.execute(
                "UPDATE weave_jobs SET state = 'void', failure_reason = ?, "
                "finished_day = ? WHERE job_id = ?",
                (reason, day, job_id),
            )
            return self.get_job(job_id)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get_loom(self, loom_id: str) -> dict:
        row = self._fetch_loom(loom_id)
        mounted = self.conn.execute(
            "SELECT shuttle_id FROM shuttles WHERE loom_id = ? ORDER BY shuttle_id",
            (loom_id,),
        ).fetchall()
        return {
            "loom_id": row["loom_id"],
            "head_side": row["head_side"],
            "created_day": row["created_day"],
            "shuttles": [r["shuttle_id"] for r in mounted],
        }

    def get_shuttle(self, shuttle_id: str) -> dict:
        row = self._fetch_shuttle(shuttle_id)
        remaining = self.remaining(shuttle_id)
        return {
            "shuttle_id": row["shuttle_id"],
            "core_id": row["core_id"],
            "color": row["color"],
            "factory_side": row["factory_side"],
            "loom_id": row["loom_id"],
            "side": row["side"],
            "state": row["state"],
            "remaining": remaining,
        }

    def remaining(self, shuttle_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(portions_delta), 0) AS n FROM shuttle_events "
            "WHERE shuttle_id = ?",
            (shuttle_id,),
        ).fetchone()
        return int(row["n"])

    def shuttle_trace(self, shuttle_id: str) -> dict:
        """Full provenance answer: remaining, side, machine and every receipt."""
        shuttle = self.get_shuttle(shuttle_id)
        events = [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "day": row["day"],
                "loom_id": row["loom_id"],
                "side": row["side"],
                "portions_delta": int(row["portions_delta"]),
                "other_loom_id": row["other_loom_id"],
                "load_id": row["load_id"],
                "run_id": row["run_id"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in self.conn.execute(
                "SELECT * FROM shuttle_events WHERE shuttle_id = ? "
                "ORDER BY rowid",
                (shuttle_id,),
            ).fetchall()
        ]
        loads = [
            {
                "load_id": row["load_id"],
                "run_id": row["run_id"],
                "bottle_id": row["bottle_id"],
                "portions": int(row["portions"]),
                "state": row["state"],
                "reserve_day": row["reserve_day"],
                "terminal_day": row["terminal_day"],
                "terminal": json.loads(row["terminal_json"])
                if row["terminal_json"]
                else None,
            }
            for row in self.conn.execute(
                "SELECT * FROM loom_loads WHERE shuttle_id = ? ORDER BY reserve_day",
                (shuttle_id,),
            ).fetchall()
        ]
        return {
            "shuttle": shuttle,
            "remaining": shuttle["remaining"],
            "current_loom": shuttle["loom_id"],
            "current_side": shuttle["side"],
            "events": events,
            "loads": loads,
        }

    def get_job(self, job_id: str) -> dict:
        job = self._fetch_job(job_id)
        steps = [
            {
                "step_index": row["step_index"],
                "row_index": row["row_index"],
                "kind": row["kind"],
                "shuttle_id": row["shuttle_id"],
                "side": row["side"],
                "color": row["color"],
                "planned_event_id": row["planned_event_id"],
                "event_id": row["event_id"],
                "status": row["status"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in self.conn.execute(
                "SELECT * FROM weave_steps WHERE job_id = ? ORDER BY step_index",
                (job_id,),
            ).fetchall()
        ]
        out = {
            "job_id": job["job_id"],
            "loom_id": job["loom_id"],
            "pattern": json.loads(job["pattern_json"]),
            "ticket_budget": int(job["ticket_budget"]),
            "tickets_used": int(job["tickets_used"]),
            "initial_head_side": job["initial_head_side"],
            "rows_done": int(job["rows_done"]),
            "state": job["state"],
            "failure_reason": job["failure_reason"],
            "created_day": job["created_day"],
            "finished_day": job["finished_day"],
            "steps": steps,
            "product": json.loads(job["product_json"])
            if job["product_json"]
            else None,
        }
        return out

    def mounted_snapshots(self, loom_id: str) -> list[ShuttleSnapshot]:
        self._fetch_loom(loom_id)
        rows = self.conn.execute(
            "SELECT * FROM shuttles WHERE loom_id = ? ORDER BY shuttle_id",
            (loom_id,),
        ).fetchall()
        return [
            ShuttleSnapshot(
                shuttle_id=row["shuttle_id"],
                color=row["color"],
                side=row["side"],
                remaining=self.remaining(row["shuttle_id"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _do_pass(
        self,
        shuttle: sqlite3.Row,
        loom: sqlite3.Row,
        day: int,
        event_id: str,
        detail: dict,
    ) -> dict:
        new_side = opposite(shuttle["side"])
        self._insert_event(
            event_id=event_id,
            shuttle_id=shuttle["shuttle_id"],
            event_type="pass",
            day=day,
            loom_id=loom["loom_id"],
            side=shuttle["side"],
            delta=-1,
            detail=detail,
        )
        new_remaining = self.remaining(shuttle["shuttle_id"])
        new_state = LOADED if new_remaining > 0 else EMPTY
        self.conn.execute(
            "UPDATE shuttles SET side = ?, state = ? WHERE shuttle_id = ?",
            (new_side, new_state, shuttle["shuttle_id"]),
        )
        self.conn.execute(
            "UPDATE looms SET head_side = ? WHERE loom_id = ?",
            (new_side, loom["loom_id"]),
        )
        return self._event_receipt(event_id)

    def _simulate_tape(self, job: sqlite3.Row) -> dict:
        """Replay every pending step against the ledger-derived facts.

        Simulated side/remaining are advanced step by step so a carry or pass
        earlier in the suffix changes the precondition checked for the next
        one; the head position is derived from the number of pass rows already
        finished.
        """
        loom = self._fetch_loom(job["loom_id"])
        expected_head = job["initial_head_side"]
        for _ in range(int(job["rows_done"])):
            expected_head = opposite(expected_head)
        if loom["head_side"] != expected_head:
            return {
                "ok": False,
                "reason": (
                    f"head drifted: tape expects {expected_head} but loom head is "
                    f"{loom['head_side']}"
                ),
            }
        pattern = json.loads(job["pattern_json"])
        steps = self.conn.execute(
            "SELECT * FROM weave_steps WHERE job_id = ? AND status = 'pending' "
            "ORDER BY step_index",
            (job["job_id"],),
        ).fetchall()

        simulated: dict[str, dict] = {}
        tickets = int(job["tickets_used"])
        for step in steps:
            shuttle = self._fetch_shuttle(step["shuttle_id"])
            if shuttle["loom_id"] != job["loom_id"]:
                return {
                    "ok": False,
                    "reason": (
                        f"shuttle {step['shuttle_id']} was handed off to "
                        f"{shuttle['loom_id']!r}"
                    ),
                }
            sim = simulated.setdefault(
                step["shuttle_id"],
                {"side": shuttle["side"], "remaining": self.remaining(shuttle["shuttle_id"])},
            )
            if step["kind"] == "carry":
                tickets += 1
                if tickets > int(job["ticket_budget"]):
                    return {
                        "ok": False,
                        "reason": (
                            f"carries need {tickets} tickets but budget is "
                            f"{job['ticket_budget']}"
                        ),
                    }
                # The step's side is the carry target; the shuttle must start
                # on the opposite side and still carry thread.
                if sim["side"] != opposite(step["side"]):
                    return {
                        "ok": False,
                        "reason": (
                            f"carry {step['planned_event_id']}: shuttle "
                            f"{step['shuttle_id']} is on {sim['side']}, tape "
                            f"expects it on {opposite(step['side'])} first"
                        ),
                    }
                if sim["remaining"] <= 0:
                    return {
                        "ok": False,
                        "reason": f"shuttle {step['shuttle_id']} has no thread",
                    }
                sim["side"] = step["side"]
                continue

            # pass step: head-side, same color, with remaining thread.
            if sim["side"] != step["side"]:
                return {
                    "ok": False,
                    "reason": (
                        f"pass row {step['row_index']}: shuttle "
                        f"{step['shuttle_id']} is on {sim['side']}, tape "
                        f"expects {step['side']}"
                    ),
                }
            if expected_head != step["side"]:
                return {
                    "ok": False,
                    "reason": (
                        f"pass row {step['row_index']}: head is on "
                        f"{expected_head}, tape expects {step['side']}"
                    ),
                }
            if shuttle["color"] != step["color"]:
                return {
                    "ok": False,
                    "reason": (
                        f"shuttle {step['shuttle_id']} color "
                        f"{shuttle['color']!r} != tape color {step['color']!r}"
                    ),
                }
            if pattern[step["row_index"]] != step["color"]:
                return {
                    "ok": False,
                    "reason": (
                        f"row {step['row_index']} pattern "
                        f"{pattern[step['row_index']]!r} != tape {step['color']!r}"
                    ),
                }
            if sim["remaining"] <= 0:
                return {
                    "ok": False,
                    "reason": f"shuttle {step['shuttle_id']} has no thread",
                }
            sim["remaining"] -= 1
            sim["side"] = opposite(sim["side"])
            expected_head = opposite(expected_head)
        return {"ok": True, "reason": None, "tickets_needed": tickets}

    def _complete_job(self, job_id: str, day: int) -> dict:
        job = self._fetch_job(job_id)
        pattern = json.loads(job["pattern_json"])
        destinations = [
            {
                "shuttle_id": row["shuttle_id"],
                "color": row["color"],
                "remaining": self.remaining(row["shuttle_id"]),
                "loom_id": row["loom_id"],
                "side": row["side"],
            }
            for row in self.conn.execute(
                "SELECT * FROM shuttles WHERE shuttle_id IN ("
                "SELECT DISTINCT shuttle_id FROM weave_steps WHERE job_id = ?) "
                "ORDER BY shuttle_id",
                (job_id,),
            ).fetchall()
        ]
        product = {
            "job_id": job_id,
            "pattern": pattern,
            "rows_woven": len(pattern),
            "tickets_used": int(job["tickets_used"]),
            "shuttle_destinations": destinations,
        }
        self.conn.execute(
            "UPDATE weave_jobs SET state = 'completed', finished_day = ?, "
            "product_json = ? WHERE job_id = ?",
            (day, json.dumps(product, sort_keys=True), job_id),
        )
        return {"job_id": job_id, "state": COMPLETED, "replayed": False, "product": product}

    def _ensure_load_method(self, color: str, portions: int, day: int) -> str:
        method_id = f"loom-load:{color}:{portions}"
        if self.conn.execute(
            "SELECT 1 FROM methods WHERE method_id = ? AND revision = 1",
            (method_id,),
        ).fetchone() is None:
            self.runs.revise_method(method_id, 1, {color: portions}, day)
        return method_id

    def _insert_event(
        self,
        event_id: str,
        shuttle_id: str,
        event_type: str,
        day: int,
        loom_id: str,
        side: str,
        delta: int,
        detail: dict,
    ) -> None:
        require_text(event_id, "receipt_id")
        self.conn.execute(
            "INSERT INTO shuttle_events(event_id, shuttle_id, event_type, day, "
            "loom_id, side, portions_delta, detail_json) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, shuttle_id, event_type, day, loom_id, side, delta,
                json.dumps(detail, sort_keys=True),
            ),
        )

    def _replay_event(self, receipt_id: str):
        row = self.conn.execute(
            "SELECT * FROM shuttle_events WHERE event_id = ?", (receipt_id,)
        ).fetchone()
        if row is None:
            return None
        receipt = self._event_receipt(receipt_id)
        receipt["replayed"] = True
        return receipt

    def _event_receipt(self, event_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM shuttle_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise LoomNotFound(f"unknown receipt {event_id!r}")
        return {
            "event_id": row["event_id"],
            "replayed": False,
            "shuttle_id": row["shuttle_id"],
            "event_type": row["event_type"],
            "day": row["day"],
            "loom_id": row["loom_id"],
            "side": row["side"],
            "portions_delta": int(row["portions_delta"]),
            "detail": json.loads(row["detail_json"]),
        }

    def _require_mounted(self, shuttle_id: str) -> sqlite3.Row:
        shuttle = self._fetch_shuttle(shuttle_id)
        if shuttle["loom_id"] is None or shuttle["side"] is None:
            raise LoomConflict(f"shuttle {shuttle_id} is not mounted on a loom")
        return shuttle

    def _require_thread(self, shuttle: sqlite3.Row) -> None:
        if self.remaining(shuttle["shuttle_id"]) <= 0:
            raise LoomConflict(
                f"shuttle {shuttle['shuttle_id']} has no remaining thread"
            )

    def _require_no_active_job(self, loom_id: str) -> None:
        row = self.conn.execute(
            "SELECT job_id FROM weave_jobs WHERE loom_id = ? "
            "AND state IN ('planned', 'running') LIMIT 1",
            (loom_id,),
        ).fetchone()
        if row is not None:
            raise LoomConflict(
                f"loom {loom_id} already has unfinished weave job {row['job_id']}"
            )

    def _load_view(self, load: sqlite3.Row, replayed: bool = False) -> dict:
        return {
            "load_id": load["load_id"],
            "run_id": load["run_id"],
            "shuttle_id": load["shuttle_id"],
            "loom_id": load["loom_id"],
            "bottle_id": load["bottle_id"],
            "color": load["color"],
            "portions": int(load["portions"]),
            "reserve_day": load["reserve_day"],
            "state": load["state"],
            "terminal_day": load["terminal_day"],
            "replayed": replayed,
        }

    def _fetch(self, table: str, key: str, value: str) -> sqlite3.Row:
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE {key} = ?", (value,)
        ).fetchone()
        if row is None:
            raise LoomNotFound(f"unknown {table[:-1]} {value!r}")
        return row

    def _fetch_optional(self, table: str, key: str, value: str):
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {key} = ?", (value,)
        ).fetchone()

    def _fetch_loom(self, loom_id: str) -> sqlite3.Row:
        return self._fetch("looms", "loom_id", require_text(loom_id, "loom_id"))

    def _fetch_shuttle(self, shuttle_id: str) -> sqlite3.Row:
        return self._fetch(
            "shuttles", "shuttle_id", require_text(shuttle_id, "shuttle_id")
        )

    def _fetch_load(self, load_id: str) -> sqlite3.Row:
        return self._fetch("loom_loads", "load_id", require_text(load_id, "load_id"))

    def _fetch_job(self, job_id: str) -> sqlite3.Row:
        return self._fetch("weave_jobs", "job_id", require_text(job_id, "job_id"))

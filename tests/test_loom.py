"""Tests for the weaving room: loads, event ledger, planner and weave tapes."""

from __future__ import annotations

import os
import tempfile
import unittest

from loom import LEFT, RIGHT, LoomService, ShuttleSnapshot, plan_tape
from loom.errors import LoomConflict, LoomNotFound, LoomValidationError
from reagent import connect, initialize
from reagent.errors import Conflict as ReagentConflict
from reagent.inventory import InventoryService
from reagent.rules import current_day

INVENTORY = {
    "initial_day": 10,
    "kinds": [
        {"kind": "R", "open_lifetime_days": 2},
        {"kind": "B", "open_lifetime_days": 2},
    ],
    "bottles": [
        {"bottle_id": "RB", "kind": "R", "total_portions": 6, "intake_day": 10,
         "printed_expiry_day": 30, "opened_day": None},
        {"bottle_id": "BB", "kind": "B", "total_portions": 3, "intake_day": 10,
         "printed_expiry_day": 11, "opened_day": None},
        {"bottle_id": "RS", "kind": "R", "total_portions": 2, "intake_day": 10,
         "printed_expiry_day": 30, "opened_day": None},
    ],
    "methods": [{"method_id": "seed", "revision": 1, "requirements": {"R": 1}}],
}


class LoomTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "store.db")
        self.conn = connect(self.db)
        initialize(self.conn)
        self.inventory = InventoryService(self.conn)
        self.inventory.initialize(INVENTORY)
        self.loom = LoomService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def register_two(self) -> None:
        self.loom.register_loom("L1", LEFT, 10)
        self.loom.register_shuttle("R", "coreR", "R", LEFT, 10)
        self.loom.register_shuttle("B", "coreB", "B", RIGHT, 10)

    def load(self, load_id, shuttle, bottle, portions, *, reserve=10, confirm=10,
             loom="L1"):
        self.loom.reserve_load(load_id, shuttle, loom, bottle, portions, reserve)
        return self.loom.confirm_load(load_id, confirm)


class RegistrationTests(LoomTestCase):
    def test_shuttle_identity_is_fixed_and_core_unique(self) -> None:
        self.register_two()
        r = self.loom.get_shuttle("R")
        self.assertEqual((r["core_id"], r["color"], r["factory_side"]),
                         ("coreR", "R", LEFT))
        with self.assertRaises(LoomConflict):
            self.loom.register_shuttle("R2", "coreR", "R", RIGHT, 10)
        with self.assertRaises(LoomConflict):
            self.loom.register_loom("L1", RIGHT, 10)

    def test_loom_capacity_is_six_shuttles(self) -> None:
        self.loom.register_loom("L1", LEFT, 10)
        for i in range(6):
            self.loom.register_shuttle(f"S{i}", f"c{i}", "R", LEFT, 10)
            self.load(f"ld{i}", f"S{i}", "RB", 1)
        self.loom.register_shuttle("S6", "c6", "R", LEFT, 10)
        with self.assertRaises(Exception):
            self.load("ld6", "S6", "RB", 1)


class LoadTests(LoomTestCase):
    def test_successful_load_docks_on_factory_side_and_backs_real_run(self) -> None:
        self.register_two()
        receipt = self.load("ldR", "R", "RB", 2)
        self.assertEqual(receipt["state"], "loaded")
        self.assertEqual(receipt["side"], LEFT)
        r = self.loom.get_shuttle("R")
        self.assertEqual(r["state"], "loaded")
        self.assertEqual(r["remaining"], 2)
        # the backing run really consumed inventory
        run = self.loom.runs.get_run("ldR")
        self.assertEqual(run["state"], "started")
        self.assertEqual(run["allocations"]["R"]["consumed"], 2)
        self.assertEqual(self.inventory.query("RB", 10)["consumed"], 2)

    def test_color_must_match_bottle_kind(self) -> None:
        self.register_two()
        with self.assertRaises(LoomValidationError):
            self.loom.reserve_load("bad", "R", "L1", "BB", 1, 10)

    def test_short_lead_reserves_nothing_and_mounts_nothing(self) -> None:
        self.register_two()
        with self.assertRaises(Exception):
            self.loom.reserve_load("big", "R", "L1", "RB", 99, 10)
        self.assertEqual(self.loom.get_shuttle("R")["state"], "registered")
        self.assertEqual(self.inventory.query("RB", 10)["reserved"], 0)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM loom_loads WHERE load_id = 'big'"
            ).fetchone()
        )

    def test_failed_start_on_expiry_mounts_no_thread_and_releases_hold(self) -> None:
        self.register_two()
        self.loom.reserve_load("ldB", "B", "L1", "BB", 1, 10)
        receipt = self.loom.confirm_load("ldB", 11)  # BB printed expiry is 11
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("expired", receipt["reason"])
        self.assertEqual(self.loom.get_shuttle("B")["state"], "registered")
        self.assertEqual(self.loom.get_shuttle("B")["remaining"], 0)
        self.assertEqual(self.inventory.query("BB", 11)["reserved"], 0)
        self.assertEqual(self.inventory.query("BB", 11)["consumed"], 0)
        # replay returns the stored failure and does not retry
        replay = self.loom.confirm_load("ldB", 11)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["state"], "failed")

    def test_reservation_and_confirmation_receipts_are_idempotent(self) -> None:
        self.register_two()
        first = self.loom.reserve_load("ldR", "R", "L1", "RB", 1, 10)
        again = self.loom.reserve_load("ldR", "R", "L1", "RB", 1, 10)
        self.assertTrue(again["replayed"])
        self.assertEqual(first["state"], again["state"])
        ok = self.loom.confirm_load("ldR", 10)
        replayed = self.loom.confirm_load("ldR", 10)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(self.loom.get_shuttle("R")["remaining"], 1)
        self.assertEqual(ok["event_id"], replayed["event_id"])


class PhysicalMoveTests(LoomTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register_two()
        self.load("ldR", "R", "RB", 2)
        self.load("ldB", "B", "BB", 1)

    def test_pass_consumes_one_and_flips_shuttle_and_head(self) -> None:
        receipt = self.loom.pass_shuttle("p1", "R", 10)
        self.assertEqual(receipt["portions_delta"], -1)
        self.assertEqual(self.loom.get_shuttle("R")["side"], RIGHT)
        self.assertEqual(self.loom.get_shuttle("R")["remaining"], 1)
        self.assertEqual(self.loom.get_loom("L1")["head_side"], RIGHT)

    def test_pass_requires_head_and_shuttle_on_same_side(self) -> None:
        # head and R are both left, B is right -> B cannot pass from left head
        with self.assertRaises(LoomConflict):
            self.loom.pass_shuttle("p-bad", "B", 10)

    def test_replay_of_move_receipt_changes_nothing(self) -> None:
        self.loom.pass_shuttle("p1", "R", 10)
        replay = self.loom.pass_shuttle("p1", "R", 10)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.loom.get_shuttle("R")["remaining"], 1)
        self.assertEqual(len(self.loom.shuttle_trace("R")["events"]), 2)

    def test_carry_flips_side_without_consumption_or_head_move(self) -> None:
        self.loom.carry_shuttle("c1", "B", 10)
        self.assertEqual(self.loom.get_shuttle("B")["side"], LEFT)
        self.assertEqual(self.loom.get_shuttle("B")["remaining"], 1)
        self.assertEqual(self.loom.get_loom("L1")["head_side"], LEFT)

    def test_empty_shuttle_can_unload_but_loaded_one_cannot(self) -> None:
        with self.assertRaises(LoomConflict):
            self.loom.unload_shuttle("u-bad", "B", 10)
        # head left, B right: pass R first to bring the head right, then B.
        self.loom.pass_shuttle("p-r", "R", 10)
        self.loom.pass_shuttle("p-b", "B", 10)
        self.assertEqual(self.loom.get_shuttle("B")["remaining"], 0)
        receipt = self.loom.unload_shuttle("u-b", "B", 10)
        self.assertEqual(receipt["event_type"], "unload")
        unloaded = self.loom.get_shuttle("B")
        self.assertEqual(unloaded["state"], "unloaded")
        self.assertIsNone(unloaded["loom_id"])

    def test_handoff_carries_remaining_thread_and_keeps_side(self) -> None:
        self.loom.register_loom("L2", LEFT, 10)
        before = self.loom.get_shuttle("R")["remaining"]
        self.loom.handoff_shuttle("h1", "R", "L2", 10)
        after = self.loom.get_shuttle("R")
        self.assertEqual(after["loom_id"], "L2")
        self.assertEqual(after["side"], LEFT)
        self.assertEqual(after["remaining"], before)
        # thread never returns to the bottle: consumed stays consumed
        self.assertEqual(self.inventory.query("RB", 10)["consumed"], 2)
        # handoff needs a different loom and is idempotent
        with self.assertRaises(LoomConflict):
            self.loom.handoff_shuttle("h2", "R", "L2", 10)
        replay = self.loom.handoff_shuttle("h1", "R", "L2", 10)
        self.assertTrue(replay["replayed"])

    def test_trace_reports_remaining_side_machine_and_every_receipt(self) -> None:
        self.loom.pass_shuttle("p1", "R", 10)
        trace = self.loom.shuttle_trace("R")
        self.assertEqual(trace["remaining"], 1)
        self.assertEqual(trace["current_loom"], "L1")
        self.assertEqual(trace["current_side"], RIGHT)
        ids = [e["event_id"] for e in trace["events"]]
        self.assertEqual(ids, ["ldR#load", "p1"])

    def test_shuttle_remaining_matches_bottle_conservation(self) -> None:
        # R loaded 2, B loaded 1; shuttle remaining is derived purely from
        # events and equals consumed bottle portions minus passes so far.
        self.loom.pass_shuttle("p1", "R", 10)
        rb = self.inventory.query("RB", 10)
        bb = self.inventory.query("BB", 10)
        self.assertEqual(rb["consumed"], 2)
        self.assertEqual(bb["consumed"], 1)
        self.assertEqual(self.loom.remaining("R"), rb["consumed"] - 1)
        self.assertEqual(self.loom.remaining("B"), bb["consumed"])
        for row in self.inventory.conservation():
            self.assertEqual(
                row["total_portions"],
                row["unallocated"] + row["reserved"] + row["consumed"],
            )

    def test_unknown_entities_raise_not_found(self) -> None:
        with self.assertRaises(LoomNotFound):
            self.loom.get_shuttle("nope")
        with self.assertRaises(LoomNotFound):
            self.loom.get_loom("nope")

    def test_event_ledger_is_append_only(self) -> None:
        import sqlite3
        self.loom.pass_shuttle("p1", "R", 10)
        with self.assertRaises(sqlite3.Error):
            self.conn.execute(
                "UPDATE shuttle_events SET portions_delta = 0 WHERE event_id = 'p1'"
            )
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("DELETE FROM shuttle_events WHERE event_id = 'p1'")

    def test_terminal_weave_job_is_immutable(self) -> None:
        import sqlite3
        self.loom.create_weave_job("j", "L1", ["R", "B", "R"], 1, 10)
        self.loom.weave_all("j", 10)
        with self.assertRaises(sqlite3.Error):
            self.conn.execute(
                "UPDATE weave_jobs SET state = 'void' WHERE job_id = 'j'"
            )


class PlannerTests(unittest.TestCase):
    def test_red_blue_red_needs_exactly_one_ticket(self) -> None:
        shuttles = [
            ShuttleSnapshot("R", "R", LEFT, 2),
            ShuttleSnapshot("B", "B", RIGHT, 1),
        ]
        plan = plan_tape(shuttles, LEFT, ["R", "B", "R"], 1)
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["tickets_needed"], 1)
        kinds = [a["kind"] for a in plan["actions"]]
        self.assertEqual(kinds, ["pass", "pass", "carry", "pass"])
        self.assertEqual(plan["final_head_side"], RIGHT)

    def test_zero_ticket_feasible_with_alternating_same_color_shuttles(self) -> None:
        shuttles = [
            ShuttleSnapshot("R1", "R", LEFT, 2),
            ShuttleSnapshot("R2", "R", LEFT, 1),
        ]
        plan = plan_tape(shuttles, LEFT, ["R", "R", "R"], 0)
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["tickets_needed"], 0)
        self.assertTrue(all(a["kind"] == "pass" for a in plan["actions"]))

    def test_budget_shortfall_and_missing_color_are_infeasible(self) -> None:
        shuttles = [
            ShuttleSnapshot("R", "R", LEFT, 2),
            ShuttleSnapshot("B", "B", RIGHT, 1),
        ]
        short = plan_tape(shuttles, LEFT, ["R", "B", "R"], 0)
        self.assertFalse(short["feasible"])
        self.assertEqual(short["tickets_needed"], 1)
        missing = plan_tape(
            [ShuttleSnapshot("R", "R", LEFT, 2)], LEFT, ["R", "G"], 4
        )
        self.assertFalse(missing["feasible"])
        self.assertIn("G", missing["reason"])

    def test_empty_shuttle_cannot_weave(self) -> None:
        plan = plan_tape(
            [ShuttleSnapshot("R", "R", LEFT, 0)], LEFT, ["R"], 0
        )
        self.assertFalse(plan["feasible"])

    def test_bounds_are_enforced(self) -> None:
        # no mounted shuttle is an infeasibility answer, not a validation error
        empty = plan_tape([], LEFT, ["R"], 0)
        self.assertFalse(empty["feasible"])
        with self.assertRaises(LoomValidationError):
            plan_tape(
                [ShuttleSnapshot("R", "R", LEFT, 9)], LEFT, ["R"] * 17, 0
            )
        with self.assertRaises(LoomValidationError):
            plan_tape(
                [ShuttleSnapshot("R", "R", LEFT, 9)], LEFT, ["R"], 5
            )
        with self.assertRaises(LoomValidationError):
            plan_tape(
                [ShuttleSnapshot("R", "R", LEFT, 9)], LEFT, [], 0
            )


class WeaveJobTests(LoomTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register_two()
        self.load("ldR", "R", "RB", 2)
        self.load("ldB", "B", "BB", 1)

    def test_job_compiles_tape_weaves_and_records_product(self) -> None:
        job = self.loom.create_weave_job("j", "L1", ["R", "B", "R"], 1, 10)
        self.assertEqual(job["state"], "planned")
        result = self.loom.weave_all("j", 10)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["product"]["tickets_used"], 1)
        stored = self.loom.get_job("j")
        self.assertTrue(all(s["status"] == "done" for s in stored["steps"]))
        # portions line up with the ledger-derived shuttle facts
        self.assertEqual(self.loom.get_shuttle("R")["remaining"], 0)
        self.assertEqual(self.loom.get_shuttle("B")["remaining"], 0)

    def test_order_rejected_when_over_budget_or_color_missing(self) -> None:
        with self.assertRaises(LoomConflict):
            self.loom.create_weave_job("j0", "L1", ["R", "B", "R"], 0, 10)
        with self.assertRaises(LoomConflict):
            self.loom.create_weave_job("jg", "L1", ["R", "G"], 1, 10)

    def test_power_cut_resumes_from_last_completed_row_without_double_spend(self) -> None:
        self.loom.create_weave_job("j", "L1", ["R", "B", "R"], 1, 10)
        self.loom.weave_next("j", 10)
        self.loom.weave_next("j", 10)
        job = self.loom.get_job("j")
        self.assertEqual(job["rows_done"], 2)
        b_remaining = self.loom.get_shuttle("B")["remaining"]
        # re-issuing the last physical receipt spends no second portion
        last_receipt = job["steps"][1]["planned_event_id"]
        replay = self.loom.pass_shuttle(last_receipt, "B", 10)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.loom.get_shuttle("B")["remaining"], b_remaining)
        # resume: one carry step then the final pass step
        carried = self.loom.weave_next("j", 10)
        self.assertEqual(carried["rows_done"], 2)
        self.assertEqual(carried["tickets_used"], 1)
        done = self.loom.weave_next("j", 10)
        self.assertEqual(done["state"], "completed")
        self.assertEqual(done["product"]["rows_woven"], 3)
        # exactly one of each portion was consumed overall
        self.assertEqual(self.inventory.query("RB", 10)["consumed"], 2)
        self.assertEqual(self.inventory.query("BB", 10)["consumed"], 1)

    def test_rehearsal_handoff_makes_verification_fail_and_blocks_weaving(self) -> None:
        self.loom.register_loom("L2", LEFT, 10)
        self.loom.register_shuttle("R2", "coreR2", "R", LEFT, 10)
        self.load("ldR2", "R2", "RS", 1)
        self.loom.create_weave_job("j", "L1", ["R", "R", "R"], 0, 10)
        self.loom.handoff_shuttle("h", "R2", "L2", 10)
        check = self.loom.verify_job("j")
        self.assertFalse(check["ok"])
        outcome = self.loom.weave_next("j", 10)
        self.assertEqual(outcome["state"], "failed")
        self.assertIn("handed off", outcome["reason"])
        # no step executed, no portion spent by the failed job
        self.assertEqual(self.loom.get_shuttle("R2")["remaining"], 1)

    def test_external_carry_invalidates_tape_but_does_not_spend_thread(self) -> None:
        self.loom.create_weave_job("j", "L1", ["R", "B", "R"], 1, 10)
        # a porter moves R to the right during rehearsal, tape expected it left
        self.loom.carry_shuttle("external", "R", 10)
        outcome = self.loom.weave_next("j", 10)
        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(self.loom.get_shuttle("R")["remaining"], 2)

    def test_void_keeps_done_rows_and_suffix_replan_finishes(self) -> None:
        # Fresh dedicated looms: five rows alternate R,B,R,B,R, so B needs 2.
        self.loom.register_loom("LV", LEFT, 10)
        self.loom.register_shuttle("RV", "coreRV", "R", LEFT, 10)
        self.loom.register_shuttle("BV", "coreBV", "B", RIGHT, 10)
        self.load("ldRV", "RV", "RB", 3, loom="LV")
        self.load("ldBV", "BV", "BB", 2, loom="LV")
        self.loom.create_weave_job("j1", "LV", ["R", "B", "R", "B", "R"], 3, 10)
        for _ in range(3):  # pass R, pass B, then the carry of R
            self.loom.weave_next("j1", 10)
        self.loom.void_job("j1", "superseded", 10)
        self.assertEqual(self.loom.get_job("j1")["rows_done"], 2)
        suffix = self.loom.create_weave_job("j2", "LV", ["B", "R"], 3, 10)
        product = self.loom.weave_all("j2", 10)["product"]
        self.assertEqual(product["rows_woven"], 2)
        self.assertEqual(product["tickets_used"], 1)

    def test_only_one_active_job_per_loom(self) -> None:
        self.loom.create_weave_job("j1", "L1", ["R", "B", "R"], 1, 10)
        with self.assertRaises(LoomConflict):
            self.loom.create_weave_job("j2", "L1", ["R"], 1, 10)

    def test_business_day_never_moves_backwards(self) -> None:
        self.loom.register_loom("LX", LEFT, 12)
        with self.assertRaises(ReagentConflict):
            self.loom.register_loom("LY", LEFT, 11)
        self.assertEqual(current_day(self.conn), 12)


if __name__ == "__main__":
    unittest.main()


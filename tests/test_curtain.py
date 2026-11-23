"""Tests for the curtain reconciliation / planner / tour service (unittest)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reagent.cli import execute_operations
from reagent.errors import Conflict, NotFoundError, ValidationError
from reagent.inventory import InventoryService
from reagent.schema import connect, initialize

from curtain import reconcile
from curtain.arrange import plan_arrangement
from curtain.cli import main as curtain_cli_main
from curtain.errors import InfeasibleRecords, NoArrangement
from curtain.schema import initialize_curtain
from curtain.service import CurtainService, onsite_options

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials" / "curtain"

INVENTORY = {
    "initial_day": 10,
    "kinds": [{"kind": "R", "open_lifetime_days": 2}],
    "bottles": [
        {
            "bottle_id": "REF",
            "kind": "R",
            "total_portions": 2,
            "intake_day": 10,
            "printed_expiry_day": 20,
            "opened_day": None,
        }
    ],
    "methods": [{"method_id": "M", "revision": 1, "requirements": {"R": 1}}],
}

STATIONS = [
    {"station_id": "A", "lo": 0, "hi": 0},
    {"station_id": "B", "lo": 0, "hi": 3},
]

# The worked example from the brief: one reference bottle read 10 on A and
# 13 on B (error 0), old seam edges read 10 on A and 14 on B (error 0).
def example_records(with_ref_b: bool = True) -> dict:
    references = [
        {"ref_id": "ref-A", "bottle_id": "REF", "station_id": "A",
         "reading": 10, "error": 0},
    ]
    if with_ref_b:
        references.append(
            {"ref_id": "ref-B", "bottle_id": "REF", "station_id": "B",
             "reading": 13, "error": 0}
        )
    return {
        "stations": [dict(s) for s in STATIONS],
        "references": references,
        "edge_readings": [
            {"reading_id": "e1@A", "edge_id": "e1", "station_id": "A",
             "reading": 10, "error": 0},
            {"reading_id": "e2@B", "edge_id": "e2", "station_id": "B",
             "reading": 14, "error": 0},
        ],
    }


def core_only_records(records: dict, core: list[str]) -> dict:
    """Records restricted to the conflicting core (station bounds stay)."""
    return {
        "stations": records["stations"],
        "references": [
            r for r in records["references"] if r["ref_id"] in core
        ],
        "edge_readings": [
            r for r in records["edge_readings"] if r["reading_id"] in core
        ],
    }


class ReconcileTests(unittest.TestCase):
    def test_pinned_offset_gives_exact_difference(self) -> None:
        rec = reconcile.Reconciler(example_records())
        self.assertEqual(rec.difference_range("e2", "e1"), (1, 1))
        witness = rec.witness()
        self.assertEqual(witness["stations"], {"A": 0, "B": 3})
        self.assertEqual(witness["bottles"], {"REF": 10})
        self.assertEqual(reconcile.verify_witness(example_records(), witness), [])

    def test_shared_offset_cancels_within_one_station(self) -> None:
        # Both edges read on B: naive interval subtraction gives [1, 7],
        # the joint interpretation keeps the shared offset and gives [4, 4].
        records = example_records(with_ref_b=False)
        records["edge_readings"] = [
            {"reading_id": "e1@B", "edge_id": "e1", "station_id": "B",
             "reading": 10, "error": 0},
            {"reading_id": "e2@B", "edge_id": "e2", "station_id": "B",
             "reading": 14, "error": 0},
        ]
        rec = reconcile.Reconciler(records)
        self.assertEqual(rec.difference_range("e2", "e1"), (4, 4))

    def test_unpinned_offset_widens_seam_range(self) -> None:
        rec = reconcile.Reconciler(example_records(with_ref_b=False))
        self.assertEqual(rec.difference_range("e2", "e1"), (1, 4))
        self.assertEqual(rec.difference_range("e1", "e2"), (-4, -1))

    def test_reading_error_widens_range(self) -> None:
        records = example_records()
        for reading in records["edge_readings"]:
            reading["error"] = 1
        rec = reconcile.Reconciler(records)
        self.assertEqual(rec.difference_range("e2", "e1"), (-1, 3))

    def test_seam_witnesses_attain_the_reported_bounds(self) -> None:
        records = example_records(with_ref_b=False)
        rec = reconcile.Reconciler(records)
        result = rec.seam_witnesses("e2", "e1")
        self.assertEqual((result["min"], result["max"]), (1, 4))
        w_min = result["witness_min"]
        w_max = result["witness_max"]
        self.assertEqual(
            w_min["edges"]["e2"] - w_min["edges"]["e1"], result["min"]
        )
        self.assertEqual(
            w_max["edges"]["e2"] - w_max["edges"]["e1"], result["max"]
        )
        self.assertEqual(reconcile.verify_witness(records, w_min), [])
        self.assertEqual(reconcile.verify_witness(records, w_max), [])

    def test_conflict_core_names_the_contradictory_readings(self) -> None:
        records = {
            "stations": [dict(s) for s in STATIONS],
            "references": [
                {"ref_id": "r1", "bottle_id": "REF", "station_id": "B",
                 "reading": 13, "error": 0},
                {"ref_id": "r2", "bottle_id": "REF", "station_id": "B",
                 "reading": 16, "error": 0},
            ],
            "edge_readings": [],
        }
        constraints = reconcile.build_constraints(records)
        core = reconcile.find_conflict_core(constraints)
        self.assertEqual(core, ["r1", "r2"])
        reduced = core_only_records(records, core)
        self.assertIsNotNone(
            reconcile.find_conflict_core(reconcile.build_constraints(reduced))
        )

    def test_conflict_core_can_involve_station_bounds(self) -> None:
        # c = 10 pinned on A, the same bottle read 20 on B forces o_B = 10,
        # outside the declared [0, 3].
        records = {
            "stations": [dict(s) for s in STATIONS],
            "references": [
                {"ref_id": "rA", "bottle_id": "REF", "station_id": "A",
                 "reading": 10, "error": 0},
                {"ref_id": "rB", "bottle_id": "REF", "station_id": "B",
                 "reading": 20, "error": 0},
            ],
            "edge_readings": [],
        }
        core = reconcile.find_conflict_core(reconcile.build_constraints(records))
        self.assertIsNotNone(core)
        self.assertIn("rA", core)
        self.assertIn("rB", core)
        reduced = core_only_records(records, core)
        self.assertIsNotNone(
            reconcile.find_conflict_core(reconcile.build_constraints(reduced))
        )

    def test_feasible_records_have_no_core(self) -> None:
        constraints = reconcile.build_constraints(example_records())
        self.assertIsNone(reconcile.find_conflict_core(constraints))


class ArrangeTests(unittest.TestCase):
    """Pure planner tests with a synthetic seam oracle."""

    @staticmethod
    def _range_fn(ranges: dict):
        def seam_range(edge_a: str, edge_b: str) -> tuple:
            return ranges.get((edge_a, edge_b), (0, 0))

        return seam_range

    def test_minimum_swaps_preferred(self) -> None:
        old = [
            {"panel_id": "O1", "left_edge": "a", "right_edge": "b"},
            {"panel_id": "O2", "left_edge": "c", "right_edge": "d"},
        ]
        spares = [
            {"panel_id": "S1", "left_edge": "x", "right_edge": "y",
             "reversible": False, "positions": [1, 2]},
        ]
        # old seam fine: zero swaps must win over any spare
        plan = plan_arrangement(old, spares, self._range_fn({}), tolerance=1)
        self.assertEqual(plan["swaps"], 0)

    def test_robust_seam_needs_worst_case_inside_tolerance(self) -> None:
        old = [
            {"panel_id": "O1", "left_edge": "a", "right_edge": "b"},
            {"panel_id": "O2", "left_edge": "c", "right_edge": "d"},
        ]
        spares = [
            {"panel_id": "S1", "left_edge": "x", "right_edge": "y",
             "reversible": False, "positions": [2]},
        ]
        ranges = {("b", "c"): (-2, 1), ("b", "x"): (0, 0)}
        plan = plan_arrangement(old, spares, self._range_fn(ranges), tolerance=1)
        self.assertEqual(plan["swaps"], 1)
        # without a permitted spare the [-2, 1] seam cannot be saved
        with self.assertRaises(NoArrangement):
            plan_arrangement(old, [], self._range_fn(ranges), tolerance=1)
        # a seam fully inside the tolerance needs no swap
        ok = {("b", "c"): (-1, 1)}
        plan = plan_arrangement(old, [], self._range_fn(ok), tolerance=1)
        self.assertEqual(plan["swaps"], 0)

    def test_spare_cannot_be_sewn_twice(self) -> None:
        old = [
            {"panel_id": "O1", "left_edge": "a", "right_edge": "b"},
            {"panel_id": "O2", "left_edge": "c", "right_edge": "d"},
            {"panel_id": "O3", "left_edge": "e", "right_edge": "f"},
        ]
        spares = [
            {"panel_id": "S1", "left_edge": "x", "right_edge": "y",
             "reversible": False, "positions": [1, 2, 3]},
        ]
        # both seams broken; the spare's right edge still breaks seam 2, so
        # no single use of the one physical spare can save both seams
        ranges = {("b", "c"): (5, 5), ("d", "e"): (5, 5), ("y", "e"): (5, 5)}
        with self.assertRaises(NoArrangement):
            plan_arrangement(old, spares, self._range_fn(ranges), tolerance=1)
        # with only seam 1 broken, one spare in the middle fixes it
        ranges_one_bad = {("b", "c"): (5, 5)}
        plan = plan_arrangement(
            old, spares, self._range_fn(ranges_one_bad), tolerance=1
        )
        self.assertEqual(plan["swaps"], 1)

    def test_only_reversible_spares_flip(self) -> None:
        old = [
            {"panel_id": "O1", "left_edge": "a", "right_edge": "b"},
            {"panel_id": "O2", "left_edge": "c", "right_edge": "d"},
        ]
        # only the flipped orientation of S1 fits the seam
        ranges = {("b", "c"): (5, 5), ("b", "x"): (5, 5), ("b", "y"): (0, 0)}
        stiff = [
            {"panel_id": "S1", "left_edge": "x", "right_edge": "y",
             "reversible": False, "positions": [2]},
        ]
        with self.assertRaises(NoArrangement):
            plan_arrangement(old, stiff, self._range_fn(ranges), tolerance=1)
        flippable = [dict(stiff[0], reversible=True)]
        plan = plan_arrangement(old, flippable, self._range_fn(ranges), tolerance=1)
        self.assertEqual(plan["swaps"], 1)
        self.assertTrue(plan["placements"][1]["flipped"])
        self.assertEqual(plan["placements"][1]["left_edge"], "y")

    def test_permissions_restrict_positions(self) -> None:
        old = [
            {"panel_id": "O1", "left_edge": "a", "right_edge": "b"},
            {"panel_id": "O2", "left_edge": "c", "right_edge": "d"},
            {"panel_id": "O3", "left_edge": "e", "right_edge": "f"},
        ]
        # the broken seam is between positions 2 and 3; a spare allowed only
        # at position 1 cannot touch it
        ranges = {("d", "e"): (5, 5)}
        misplaced = [
            {"panel_id": "S1", "left_edge": "x", "right_edge": "y",
             "reversible": False, "positions": [1]},
        ]
        with self.assertRaises(NoArrangement):
            plan_arrangement(old, misplaced, self._range_fn(ranges), tolerance=1)
        allowed = [dict(misplaced[0], positions=[3])]
        plan = plan_arrangement(old, allowed, self._range_fn(ranges), tolerance=1)
        self.assertEqual(plan["swaps"], 1)

    def test_available_restricts_the_pool(self) -> None:
        old = [
            {"panel_id": "O1", "left_edge": "a", "right_edge": "b"},
            {"panel_id": "O2", "left_edge": "c", "right_edge": "d"},
        ]
        spares = [
            {"panel_id": "S1", "left_edge": "x", "right_edge": "y",
             "reversible": False, "positions": [2]},
        ]
        ranges = {("b", "c"): (5, 5)}
        with self.assertRaises(NoArrangement):
            plan_arrangement(
                old, spares, self._range_fn(ranges), tolerance=1, available=set()
            )


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "store.db")
        self.conn = connect(self.db_path)
        initialize(self.conn)
        initialize_curtain(self.conn)
        self.inventory = InventoryService(self.conn)
        self.curtain = CurtainService(self.conn)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmp.cleanup()

    def init_store(self, document: dict | None = None) -> None:
        self.inventory.initialize(document or INVENTORY)

    def run_operations(self, name: str) -> None:
        operations = json.loads((MATERIALS / name).read_text("utf-8"))
        execute_operations(self.conn, operations)

    def real_checkouts(self) -> None:
        """Both reference runs consume one portion each (days 12 and 13)."""
        self.run_operations("operations_real.json")

    def expired_checkouts(self) -> None:
        """The second checkout fails: opened day 12 + L2 expires on day 14."""
        self.run_operations("operations_expired.json")

    def setup_curtain(self, day: int = 13) -> None:
        document = json.loads((MATERIALS / "curtain.json").read_text("utf-8"))
        self.curtain.setup(document, day)

    def register_two_references(self, day: int = 13) -> None:
        self.curtain.register_reference("ref-A", "REF", "A", 10, 0, day)
        self.curtain.register_reference("ref-B", "REF", "B", 13, 0, day)


class ReferenceBackingTests(StoreTestCase):
    def test_two_consumed_portions_back_two_references(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        self.assertEqual(
            self.curtain.get_reference("ref-B")["status"], "registered"
        )

    def test_third_reference_exceeds_consumed_portions(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        with self.assertRaises(Conflict):
            self.curtain.register_reference("ref-C", "REF", "B", 12, 0, 13)

    def test_failed_checkout_backs_no_reference(self) -> None:
        self.init_store()
        self.expired_checkouts()
        self.setup_curtain(day=14)
        self.curtain.register_reference("ref-A", "REF", "A", 10, 0, 14)
        with self.assertRaises(Conflict):
            self.curtain.register_reference("ref-B", "REF", "B", 13, 0, 14)

    def test_revoke_and_reapprove(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        self.curtain.revoke_reference("ref-B", 14)
        self.assertEqual(self.curtain.get_reference("ref-B")["status"], "revoked")
        with self.assertRaises(Conflict):
            self.curtain.revoke_reference("ref-B", 14)
        self.curtain.reapprove_reference("ref-B", 15)
        self.assertEqual(
            self.curtain.get_reference("ref-B")["status"], "registered"
        )
        with self.assertRaises(Conflict):
            self.curtain.reapprove_reference("ref-B", 15)

    def test_unknown_bottle_or_station_rejected(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        with self.assertRaises(NotFoundError):
            self.curtain.register_reference("r1", "NOPE", "A", 10, 0, 13)
        with self.assertRaises(NotFoundError):
            self.curtain.register_reference("r2", "REF", "NOPE", 10, 0, 13)


class CountersignTests(StoreTestCase):
    def test_countersign_is_content_addressed(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        first = self.curtain.countersign(13)
        self.assertTrue(first["feasible"])
        self.assertFalse(first["existing"])
        self.assertEqual(first["witness"]["stations"]["B"], 3)
        again = self.curtain.countersign(14)
        self.assertEqual(again["revision_id"], first["revision_id"])
        self.assertTrue(again["existing"])

    def test_conflicting_records_are_signed_with_their_core(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.curtain.register_reference("r1", "REF", "B", 13, 0, 13)
        self.curtain.register_reference("r2", "REF", "B", 16, 0, 13)
        revision = self.curtain.countersign(13)
        self.assertFalse(revision["feasible"])
        self.assertEqual(revision["core"], ["r1", "r2"])
        with self.assertRaises(InfeasibleRecords) as ctx:
            self.curtain.seam_query(revision["revision_id"], "eO1R", "eO2L")
        self.assertEqual(ctx.exception.core, ["r1", "r2"])

    def test_unread_edge_cannot_be_countersigned(self) -> None:
        self.init_store()
        self.real_checkouts()
        document = json.loads((MATERIALS / "curtain.json").read_text("utf-8"))
        document["edges"]["GHOST"] = []
        # edges without readings are already rejected at setup time
        with self.assertRaises(ValidationError):
            self.curtain.setup(document, 13)

    def test_countersign_before_any_sewing_order(self) -> None:
        # the measurement room can countersign while no show exists yet
        self.init_store()
        self.real_checkouts()
        document = json.loads((MATERIALS / "curtain.json").read_text("utf-8"))
        document["shows"] = []
        self.curtain.setup(document, 13)
        self.register_two_references()
        revision = self.curtain.countersign(13)
        self.assertTrue(revision["feasible"])
        seam = self.curtain.seam_query(revision["revision_id"], "eO2L", "eO1R")
        self.assertEqual((seam["min"], seam["max"]), (1, 1))

    def test_revision_rows_are_immutable(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        revision = self.curtain.countersign(13)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE curtain_revisions SET feasible = 0 WHERE revision_id = ?",
                (revision["revision_id"],),
            )


class WorkedExampleTests(StoreTestCase):
    """The three scenarios from the brief, end to end on the store."""

    def test_example1_tolerance_one_hangs_directly(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        revision = self.curtain.countersign(13)
        seam = self.curtain.seam_query(revision["revision_id"], "eO2L", "eO1R")
        self.assertEqual((seam["min"], seam["max"]), (1, 1))
        plan = self.curtain.plan("ex", revision["revision_id"])
        self.assertEqual(plan["swaps"], 0)

    def test_example2_expired_checkout_forces_one_swap(self) -> None:
        self.init_store()
        self.expired_checkouts()
        self.setup_curtain(day=14)
        self.curtain.register_reference("ref-A", "REF", "A", 10, 0, 14)
        with self.assertRaises(Conflict):
            self.curtain.register_reference("ref-B", "REF", "B", 13, 0, 14)
        revision = self.curtain.countersign(14)
        seam = self.curtain.seam_query(revision["revision_id"], "eO2L", "eO1R")
        self.assertEqual((seam["min"], seam["max"]), (1, 4))
        plan = self.curtain.plan("ex", revision["revision_id"])
        self.assertEqual(plan["swaps"], 1)
        self.assertEqual(plan["placements"][1]["panel_id"], "S0")

    def test_example3_tolerance_zero_forces_one_swap(self) -> None:
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        revision = self.curtain.countersign(13)
        plan = self.curtain.plan("ex0", revision["revision_id"])
        self.assertEqual(plan["swaps"], 1)
        self.assertEqual(plan["placements"][1]["panel_id"], "S0")
        self.assertEqual(plan["seams"][0]["min"], 0)
        self.assertEqual(plan["seams"][0]["max"], 0)


class TourTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_store()
        self.real_checkouts()
        self.setup_curtain()
        self.register_two_references()
        self.rev1 = self.curtain.countersign(13)["revision_id"]

    def seal_tour(self) -> dict:
        return self.curtain.seal_loading_list(
            "tour-list", self.rev1, ["T1", "T2"], ["S1", "S2", "S3", "S4"], 16
        )

    def test_seal_computes_primary_and_contingencies(self) -> None:
        doc = self.seal_tour()
        t1 = doc["shows"]["T1"]
        self.assertEqual(t1["primary"]["swaps"], 1)
        self.assertEqual(t1["primary"]["placements"][1]["panel_id"], "S1")
        contingency = t1["contingencies"]["S1"]
        self.assertEqual(contingency["swaps"], 2)
        used = {
            (p["position"], p["panel_id"])
            for p in contingency["placements"]
            if p["choice"] == "spare"
        }
        self.assertEqual(used, {(2, "S3"), (3, "S2")})
        # spares the primary does not use fall back to the primary itself
        self.assertEqual(t1["contingencies"]["S2"], t1["primary"])
        t2 = doc["shows"]["T2"]
        self.assertEqual(t2["primary"]["placements"][1]["panel_id"], "S1")
        self.assertEqual(
            t2["contingencies"]["S1"]["placements"][1]["panel_id"], "S4"
        )

    def test_seal_requires_a_feasible_revision(self) -> None:
        self.curtain.revoke_reference("ref-B", 14)
        self.curtain.revoke_reference("ref-A", 14)
        self.curtain.register_reference("r1", "REF", "B", 13, 0, 14)
        self.curtain.register_reference("r2", "REF", "B", 16, 0, 14)
        bad = self.curtain.countersign(14)
        self.assertFalse(bad["feasible"])
        with self.assertRaises(InfeasibleRecords):
            self.curtain.seal_loading_list("L", bad["revision_id"], ["T1"], ["S1"], 15)

    def test_seal_with_unloadable_requirement_fails(self) -> None:
        # under tolerance 1 with o_B in [0, 3] the old seam cannot hold and
        # no spare is loaded: the witness would not match the physical load
        self.curtain.revoke_reference("ref-B", 14)
        rev2 = self.curtain.countersign(14)["revision_id"]
        with self.assertRaises(NoArrangement):
            self.curtain.seal_loading_list("empty", rev2, ["ex"], [], 15)

    def test_seal_rejects_locked_or_unknown_spares(self) -> None:
        self.seal_tour()
        self.curtain.depart("tour-list", 16)
        self.curtain.handover("tour-list", "T1", "primary",
                              ["S1", "S2", "S3", "S4"], 17)
        with self.assertRaises(Conflict):
            self.curtain.seal_loading_list("second", self.rev1, ["T2"], ["S1"], 18)
        with self.assertRaises(NotFoundError):
            self.curtain.seal_loading_list("third", self.rev1, ["T2"], ["NOPE"], 18)

    def test_onsite_is_a_pure_function_of_the_sealed_document(self) -> None:
        doc = self.seal_tour()
        self.curtain.depart("tour-list", 16)
        # S2 lost after loading: the crew evaluates the sealed document only
        answer = onsite_options(doc, ["S1", "S3", "S4"])
        self.assertTrue(answer["T1"]["can_deliver"])
        self.assertEqual(answer["T1"]["options"][0]["key"], "primary")
        # losing S1 as well leaves T1 undeliverable
        answer = onsite_options(doc, ["S3", "S4"])
        self.assertFalse(answer["T1"]["can_deliver"])
        self.assertEqual(answer["T1"]["options"], [])

    def test_handover_locks_and_competing_arrangements(self) -> None:
        self.seal_tour()
        self.curtain.depart("tour-list", 16)
        receipt = self.curtain.handover(
            "tour-list", "T1", "primary", ["S1", "S3", "S4"], 17
        )
        self.assertEqual(receipt["locked"], ["S1"])
        # T2's sealed primary wants the same physical S1: already locked
        with self.assertRaises(Conflict):
            self.curtain.handover("tour-list", "T2", "primary",
                                  ["S1", "S3", "S4"], 18)
        # the sealed contingency for missing S1 uses S4 instead
        receipt = self.curtain.handover(
            "tour-list", "T2", "missing:S1", ["S3", "S4"], 18
        )
        self.assertEqual(receipt["locked"], ["S4"])
        locks = {row["panel_id"]: row["show_id"] for row in self.curtain.locks()}
        self.assertEqual(locks, {"S1": "T1", "S4": "T2"})
        with self.assertRaises(Conflict):
            self.curtain.handover("tour-list", "T2", "primary", ["S3"], 19)

    def test_handover_requires_departed_and_present_spares(self) -> None:
        self.seal_tour()
        with self.assertRaises(Conflict):
            self.curtain.handover("tour-list", "T1", "primary",
                                  ["S1", "S2", "S3", "S4"], 16)
        self.curtain.depart("tour-list", 16)
        with self.assertRaises(Conflict):
            self.curtain.handover("tour-list", "T1", "primary", ["S3", "S4"], 17)
        with self.assertRaises(ValidationError):
            self.curtain.handover("tour-list", "T1", "bogus",
                                  ["S1", "S2", "S3", "S4"], 17)

    def test_reapproval_keeps_the_old_list_as_sealed(self) -> None:
        self.curtain.revoke_reference("ref-B", 14)
        rev2 = self.curtain.countersign(14)["revision_id"]
        old_list = self.curtain.seal_loading_list("ex-old", rev2, ["ex"], ["S0"], 14)
        self.assertEqual(old_list["shows"]["ex"]["primary"]["swaps"], 1)
        self.curtain.reapprove_reference("ref-B", 15)
        rev3 = self.curtain.countersign(15)
        self.assertEqual(rev3["revision_id"], self.rev1)
        new_list = self.curtain.seal_loading_list(
            "ex-new", rev3["revision_id"], ["ex"], ["S0"], 15
        )
        self.assertEqual(new_list["shows"]["ex"]["primary"]["swaps"], 0)
        kept = self.curtain.get_list("ex-old")
        self.assertEqual(kept["revision_id"], rev2)
        self.assertEqual(kept["shows"]["ex"]["primary"]["swaps"], 1)
        # the old revision still answers its own seam question
        seam = self.curtain.seam_query(rev2, "eO2L", "eO1R")
        self.assertEqual((seam["min"], seam["max"]), (1, 4))

    def test_loading_list_content_is_immutable(self) -> None:
        self.seal_tour()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE curtain_loading_lists SET loaded_json = '[]' "
                "WHERE list_id = 'tour-list'"
            )

    def test_original_inventory_queries_still_work(self) -> None:
        self.seal_tour()
        alloc = self.inventory.query("REF", 13)
        self.assertEqual(alloc["consumed"], 2)
        self.assertEqual(alloc["unallocated"], 0)
        self.assertEqual(alloc["opened_day"], 12)
        conservation = self.inventory.conservation()
        self.assertEqual(
            conservation,
            [{"bottle_id": "REF", "total_portions": 2, "unallocated": 0,
              "reserved": 0, "consumed": 2}],
        )


class CliTests(StoreTestCase):
    def test_cli_flow(self) -> None:
        self.init_store()
        self.real_checkouts()
        curtain = MATERIALS / "curtain.json"
        self.assertEqual(
            curtain_cli_main(
                ["--db", self.db_path, "setup", "--document", str(curtain),
                 "--day", "13"]
            ),
            0,
        )
        self.assertEqual(
            curtain_cli_main(
                ["--db", self.db_path, "register-ref", "--ref-id", "ref-A",
                 "--bottle", "REF", "--station", "A", "--reading", "10",
                 "--error", "0", "--day", "13"]
            ),
            0,
        )
        self.assertEqual(
            curtain_cli_main(
                ["--db", self.db_path, "register-ref", "--ref-id", "ref-B",
                 "--bottle", "REF", "--station", "B", "--reading", "13",
                 "--error", "0", "--day", "13"]
            ),
            0,
        )
        self.assertEqual(
            curtain_cli_main(["--db", self.db_path, "countersign", "--day", "13"]),
            0,
        )
        revision = self.curtain.countersign(14)["revision_id"]
        self.assertEqual(
            curtain_cli_main(
                ["--db", self.db_path, "seam", "--revision", revision,
                 "--edge-a", "eO2L", "--edge-b", "eO1R"]
            ),
            0,
        )
        self.assertEqual(
            curtain_cli_main(
                ["--db", self.db_path, "plan", "--show", "ex",
                 "--revision", revision]
            ),
            0,
        )
        # failing command exits 2 and does not crash
        self.assertEqual(
            curtain_cli_main(
                ["--db", self.db_path, "seam", "--revision", revision,
                 "--edge-a", "NOPE", "--edge-b", "eO1R"]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()

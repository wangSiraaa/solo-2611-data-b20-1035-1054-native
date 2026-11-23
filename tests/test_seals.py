"""Tests for the seal-label / mold-archive / freight layer.

Covers, in the order of the task statement:

* only really-consumed named bottles / reservations / portion ordinals can
  press a label (reserved-only, expired-unstarted, repeats never over-issue);
* complementary break-line reconstruction, worn cells, segments, vouchers,
  reverse reading and splice-mark evidence;
* reusable molds, versioned re-reads, stop-order blockers;
* minimum-work-hours freight plans, customs locks, co-return contract limits;
* the cabinet-first workflow, break-receipt persistence, reopen after a lost
  return packet and the no-double-disposition guarantee.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from reagent.errors import Conflict, NotFoundError, ValidationError
from reagent.inventory import InventoryService
from reagent.runs import RunService
from reagent.schema import connect, initialize

from seals import (
    ArchiveService,
    CostModel,
    HOLE_MISSING,
    HOLE_PRESENT,
    Imprint,
    Mold,
    PlanBlocked,
    SealLabelService,
    Voucher,
    WorkflowService,
    build_tree,
    classify_leaves,
    initialize_seals,
    plan_dispositions,
)
from seals.matching import reconstruct

WAX_DOC = {
    "initial_day": 10,
    "kinds": [{"kind": "WAX", "open_lifetime_days": 5}],
    "bottles": [
        {
            "bottle_id": "W1",
            "kind": "WAX",
            "total_portions": 4,
            "intake_day": 10,
            "printed_expiry_day": 30,
            "opened_day": None,
        },
        {
            "bottle_id": "W2",
            "kind": "WAX",
            "total_portions": 1,
            "intake_day": 10,
            "printed_expiry_day": 12,
            "opened_day": None,
        },
    ],
    "methods": [
        {"method_id": "seal", "revision": 1, "requirements": {"WAX": 1}},
        {"method_id": "seal", "revision": 2, "requirements": {"WAX": 2}},
    ],
}


class SealStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "store.db")
        self.conn = connect(self.db_path)
        initialize(self.conn)
        initialize_seals(self.conn)
        self.inventory = InventoryService(self.conn)
        self.inventory.initialize(WAX_DOC)
        self.runs = RunService(self.conn)
        self.seals = SealLabelService(self.conn)
        self.archive = ArchiveService(self.conn)
        self.workflow = WorkflowService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def start_run(self, run_id: str, revision: int, bottle: str, portions: int,
                  create_day: int = 10, start_day: int = 11) -> None:
        self.runs.create_run(
            run_id,
            "seal",
            revision,
            create_day,
            [{"bottle_id": bottle, "portions": portions}],
        )
        self.runs.start_run(run_id, start_day)


# ---------------------------------------------------------------------------
# pure matching engine
# ---------------------------------------------------------------------------
class MatchingEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mold_a = Mold("mA", "A", "00")
        self.mold_b = Mold("mB", "B", "01")

    def test_complement_bits_match_only_right_mold(self) -> None:
        sol_a, fail_a = reconstruct(
            Imprint(("11",), frozenset(), HOLE_PRESENT),
            (self.mold_a, self.mold_b),
            (),
        )
        self.assertEqual([s.mold_id for s in sol_a], ["mA"])
        self.assertEqual([f.mold_id for f in fail_a], ["mB"])

        sol_b, fail_b = reconstruct(
            Imprint(("10",), frozenset(), HOLE_PRESENT),
            (self.mold_a, self.mold_b),
            (),
        )
        self.assertEqual([s.mold_id for s in sol_b], ["mB"])
        self.assertEqual([f.mold_id for f in fail_b], ["mA"])

    def test_worn_cell_matches_either_bit_and_keeps_a_position(self) -> None:
        solutions, _ = reconstruct(
            Imprint(("1?",), frozenset(), HOLE_PRESENT),
            (self.mold_a, self.mold_b),
            (),
        )
        self.assertEqual(
            sorted(s.mold_id for s in solutions), ["mA", "mB"]
        )
        # the worn cell still occupies one tooth: spans are length 2
        for solution in solutions:
            (placement,) = solution.placements
            self.assertEqual(placement.mold_end - placement.mold_start, 1)

    def test_voucher_span_on_four_tooth_mold(self) -> None:
        mold = Mold("m4", "C", "0110")
        voucher = Voucher("v1", "m4", "s1", 1, 2, 2, 2)
        imprint = Imprint(("1", "1"), frozenset(), HOLE_PRESENT)

        solutions, failures = reconstruct(imprint, (mold,), (voucher,))
        self.assertEqual(len(solutions), 1)
        solution = solutions[0]
        self.assertEqual(
            [(p.mold_start, p.mold_end) for p in solution.placements],
            [(0, 0), (3, 3)],
        )
        self.assertEqual(solution.gaps[0].skip, 2)
        self.assertEqual(solution.gaps[0].voucher_id, "v1")

        # remove the trim evidence: mold 0110 has no adjacent pair of 0s, so
        # the two imprint "1" cells cannot sit on a continuous segment
        no_voucher, fail = reconstruct(imprint, (mold,), ())
        self.assertEqual(no_voucher, [])
        self.assertTrue(
            any("waste voucher" in reason for reason in fail[0].reasons)
        )

    def test_segments_cannot_skip_teeth_without_voucher(self) -> None:
        # even a bit-compatible split placement is illegal without evidence
        mold = Mold("m", "C", "0000")
        imprint = Imprint(("1", "1"), frozenset(), HOLE_PRESENT)
        solutions, failures = reconstruct(imprint, (mold,), ())
        self.assertEqual(solutions, [])
        self.assertTrue(failures)

    def test_reverse_reading_requires_missing_locating_hole(self) -> None:
        # mold 10 only matches imprint 10 when read in reverse
        mold = Mold("mr", "R", "10")
        imprint = Imprint(("10",), frozenset(), HOLE_MISSING)
        solutions, _ = reconstruct(imprint, (mold,), ())
        self.assertEqual([s.direction for s in solutions], ["reverse"])

        blocked, failures = reconstruct(
            Imprint(("10",), frozenset(), HOLE_PRESENT), (mold,), ()
        )
        self.assertEqual(blocked, [])
        self.assertTrue(
            all("forward" in reason for reason in failures[0].reasons)
        )

    def test_false_splice_mark_invalidates_the_position(self) -> None:
        # a markless continuous mold segment cannot satisfy a claimed mark
        mold = Mold("m5", "D", "000")
        solutions, failures = reconstruct(
            Imprint(("11",), frozenset({0}), HOLE_PRESENT), (mold,), ()
        )
        self.assertEqual(solutions, [])
        evidence = failures[0].reasons
        self.assertTrue(any("splice disagreement" in e for e in evidence))
        self.assertTrue(any("mold adjacency 0" in e for e in evidence))

    def test_agreed_splice_marks_are_accepted(self) -> None:
        mold = Mold("m6", "E", "000", frozenset({0}))
        solutions, failures = reconstruct(
            Imprint(("11",), frozenset({0}), HOLE_PRESENT), (mold,), ()
        )
        self.assertEqual(len(solutions), 1)
        self.assertEqual(failures, [])

    def test_same_mold_segment_serves_two_imprints(self) -> None:
        mold = Mold("m", "A", "00")
        first, _ = reconstruct(
            Imprint(("11",), frozenset(), HOLE_PRESENT), (mold,), ()
        )
        second, _ = reconstruct(
            Imprint(("1?",), frozenset(), HOLE_PRESENT), (mold,), ()
        )
        self.assertEqual(first[0].placements, second[0].placements)

    def test_segment_boundary_cannot_carry_a_splice_mark(self) -> None:
        # flat cells are 0..3; the gap between the two 2-cell segments is the
        # adjacency after flat cell 1, i.e. local adjacency 1
        with self.assertRaises(ValueError):
            reconstruct(
                Imprint(("10", "10"), frozenset({1}), HOLE_PRESENT),
                (Mold("m", "A", "000000"),),
                (),
            )

    def test_multi_segment_marks_map_per_segment_not_across_gap(self) -> None:
        # teeth 0,1 hold the first "10" segment, teeth 3,4 the second one;
        # the readable adjacencies are mold adjacencies 0 and 3
        mold = Mold("mm", "B", "010010", splice_adjacencies=frozenset({0, 3}))
        voucher = Voucher("v1", "mm", "s", 2, 2, 1, 1)
        imprint = Imprint(
            ("10", "10"), frozenset({0, 2}), HOLE_PRESENT
        )
        solutions, failures = reconstruct(imprint, (mold,), (voucher,))
        self.assertEqual(len(solutions), 1)
        self.assertEqual(
            [(p.mold_start, p.mold_end) for p in solutions[0].placements],
            [(0, 1), (3, 4)],
        )
        self.assertEqual(failures, [])

        # dropping the second imprint mark must surface the mold's mark at
        # adjacency 3 as the evidence that invalidates the position
        under_marked = Imprint(("10", "10"), frozenset({0}), HOLE_PRESENT)
        solutions, failures = reconstruct(under_marked, (mold,), (voucher,))
        self.assertEqual(solutions, [])
        self.assertTrue(
            any("mold adjacency 3" in e for e in failures[0].reasons)
        )

    def test_skip_count_must_stay_inside_voucher_interval(self) -> None:
        mold = Mold("mm", "B", "010010")
        # the only bit-compatible gap skips tooth 2 alone (gap (1, 3))
        only_one = Voucher("v1", "mm", "s", 2, 2, 1, 1)
        up_to_two = Voucher("v2", "mm", "s", 2, 2, 1, 2)
        imprint = Imprint(("10", "10"), frozenset(), HOLE_PRESENT)
        one_solution, _ = reconstruct(imprint, (mold,), (only_one,))
        self.assertEqual([s.gaps[0].skip for s in one_solution], [1])
        # a voucher allowing more teeth still only admits the bit-compatible
        # skip-1 gap on this mold
        two_solutions, _ = reconstruct(imprint, (mold,), (up_to_two,))
        self.assertEqual(
            sorted(s.gaps[0].skip for s in two_solutions), [1]
        )
        # a voucher demanding two skipped teeth cannot cover this one-tooth gap
        only_two = Voucher("v3", "mm", "s", 2, 2, 2, 2)
        none_solution, failures = reconstruct(imprint, (mold,), (only_two,))
        self.assertEqual(none_solution, [])
        self.assertTrue(
            any("waste voucher" in e for e in failures[0].reasons)
        )


# ---------------------------------------------------------------------------
# label issuance against the real reagent service
# ---------------------------------------------------------------------------
class LabelIssuanceTests(SealStoreTestCase):
    def test_only_started_runs_press_labels_one_portion_each(self) -> None:
        self.start_run("r1", 2, "W1", 2)
        first = self.seals.issue_label("L1", "box-1", "r1", 11)
        second = self.seals.issue_label("L2", "box-2", "r1", 11)
        self.assertEqual(first["portion_seq"], 1)
        self.assertEqual(second["portion_seq"], 2)
        self.assertEqual(first["reservation_id"], second["reservation_id"])
        # both portions are now spoken for; a third strip cannot be pressed
        with self.assertRaises(Conflict):
            self.seals.issue_label("L3", "box-3", "r1", 11)

    def test_reserved_but_unstarted_run_cannot_press_label(self) -> None:
        self.runs.create_run(
            "r-res", "seal", 1, 10, [{"bottle_id": "W1", "portions": 1}]
        )
        with self.assertRaises(Conflict):
            self.seals.issue_label("Lx", "box-x", "r-res", 11)
        # the failed scan is archived and no label exists
        attempts = self.conn.execute(
            "SELECT outcome FROM label_attempts WHERE box_id = 'box-x'"
        ).fetchall()
        self.assertEqual([r["outcome"] for r in attempts], ["reserved_only"])
        self.assertEqual(self.seals.list_labels(), [])

    def test_expired_run_releases_and_cannot_press_label(self) -> None:
        self.runs.create_run(
            "r-exp", "seal", 1, 10, [{"bottle_id": "W2", "portions": 1}]
        )
        result = self.runs.start_run("r-exp", 12)  # expiry day 12 exclusive
        self.assertEqual(result["state"], "released")
        with self.assertRaises(Conflict):
            self.seals.issue_label("Le", "box-e", "r-exp", 12)

    def test_repeat_scan_never_issues_a_second_strip(self) -> None:
        self.start_run("r1", 1, "W1", 1)
        self.seals.issue_label("L1", "box-1", "r1", 11)
        with self.assertRaises(Conflict):
            self.seals.issue_label("L1-DUP", "box-1", "r1", 11)
        labels = self.seals.list_labels()
        self.assertEqual(len(labels), 1)
        attempts = self.conn.execute(
            "SELECT outcome FROM label_attempts WHERE box_id = 'box-1' "
            "ORDER BY scan_id"
        ).fetchall()
        self.assertEqual(
            [r["outcome"] for r in attempts], ["issued", "duplicate"]
        )

    def test_repeat_start_replays_receipt_without_extra_consumption(self) -> None:
        self.start_run("r1", 1, "W1", 1)
        first = self.runs.start_run("r1", 12)
        second = self.runs.start_run("r1", 13)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["consumed"], second["consumed"])
        # one consumed portion still means exactly one label is possible
        self.seals.issue_label("L1", "box-1", "r1", 13)
        with self.assertRaises(Conflict):
            self.seals.issue_label("L2", "box-2", "r1", 13)


# ---------------------------------------------------------------------------
# archive: molds, vouchers, versioned imprints
# ---------------------------------------------------------------------------
class ArchiveTests(SealStoreTestCase):
    def _two_labels(self) -> tuple[str, str]:
        self.start_run("r1", 2, "W1", 2)
        self.seals.issue_label("LX", "X", "r1", 11)
        self.seals.issue_label("LY", "Y", "r1", 11)
        return "LX", "LY"

    def test_reconstruction_query_reports_batches_segments_and_vouchers(self) -> None:
        lx, ly = self._two_labels()
        self.archive.register_mold("mA", "A", "00", 12)
        self.archive.register_mold("mB", "B", "01", 12)
        self.archive.record_imprint("I-X", lx, ["11"], 12)
        self.archive.record_imprint("I-Y", ly, ["10"], 12)

        query_x = self.archive.reconstruct_imprint("I-X")
        self.assertEqual(query_x["possible_batches"], ["A"])
        self.assertEqual(query_x["solutions"][0]["mold_id"], "mA")
        query_y = self.archive.reconstruct_label(ly)
        self.assertEqual(
            query_y["versions"][0]["possible_batches"], ["B"]
        )

    def test_reread_with_new_cells_creates_a_new_version(self) -> None:
        lx, _ = self._two_labels()
        self.archive.register_mold("mA", "A", "00", 12)
        self.archive.register_mold("mB", "B", "01", 12)
        self.archive.record_imprint("I-X", lx, ["11"], 12)
        self.archive.record_imprint("I-X", lx, ["1?"], 13, note="worn")

        self.assertEqual(
            self.archive.reconstruct_imprint("I-X", 1)["possible_batches"],
            ["A"],
        )
        self.assertEqual(
            self.archive.reconstruct_imprint("I-X", 2)["possible_batches"],
            ["A", "B"],
        )
        self.assertEqual(self.seals.get_label(lx)["current_version"], 2)
        # the old version remains on file
        self.assertEqual(
            self.archive.get_imprint("I-X", 1)["segments"], ["11"]
        )

    def test_imprint_requires_a_legal_label(self) -> None:
        with self.assertRaises(NotFoundError):
            self.archive.record_imprint("I-Z", "GHOST", ["11"], 12)

    def test_mold_pattern_and_vouchers_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            self.archive.register_mold("bad", "B", "021", 12)
        self.archive.register_mold("mA", "A", "0000", 12)
        with self.assertRaises(ValidationError):
            self.archive.register_voucher(
                "v", "mA", "s1", 0, 3, 5, 5  # skip larger than the span
            )

    def test_reconstruction_rows_are_persisted_for_archive_queries(self) -> None:
        lx, _ = self._two_labels()
        self.archive.register_mold("mA", "A", "00", 12)
        self.archive.record_imprint("I-X", lx, ["11"], 12)
        self.archive.reconstruct_imprint("I-X")
        rows = self.conn.execute(
            "SELECT mold_id FROM imprint_reconstructions"
        ).fetchall()
        self.assertEqual([r["mold_id"] for r in rows], ["mA"])

    def test_archive_round_trips_segmented_read_with_voucher(self) -> None:
        lx, _ = self._two_labels()
        self.archive.register_mold(
            "m6", "C", "010010", 12, splice_adjacencies=[0, 3]
        )
        self.archive.register_voucher("v1", "m6", "s1", 2, 2, 1, 1)
        self.archive.record_imprint(
            "I-seg", lx, ["10", "10"], 12, splice_marks=[0, 2]
        )
        result = self.archive.reconstruct_imprint("I-seg")
        self.assertTrue(result["resolvable"])
        self.assertEqual(result["possible_batches"], ["C"])
        self.assertEqual(
            [(s["mold_start"], s["mold_end"]) for s in result["solutions"][0]["segments"]],
            [(0, 1), (3, 4)],
        )
        self.assertEqual(
            result["solutions"][0]["gaps"][0]["voucher_id"], "v1"
        )


# ---------------------------------------------------------------------------
# freight planning: the worked example and its variations
# ---------------------------------------------------------------------------
CABINET_TREE = [
    {"node_id": "CAB", "kind": "cabinet"},
    {"node_id": "X", "kind": "leaf", "parent_id": "CAB", "label_id": "LX"},
    {"node_id": "Y", "kind": "leaf", "parent_id": "CAB", "label_id": "LY"},
]
COSTS = CostModel(sealed_return={"CAB": 4}, unseal={"CAB": 1}, leaf_charge=2)


class FreightPlanningTests(SealStoreTestCase):
    def _issue_and_read(self, x_segments: list[str], y_segments: list[str]) -> None:
        self.start_run("r1", 2, "W1", 2)
        self.seals.issue_label("LX", "X", "r1", 11)
        self.seals.issue_label("LY", "Y", "r1", 11)
        self.archive.register_mold("mA", "A", "00", 12)
        self.archive.register_mold("mB", "B", "01", 12)
        self.archive.record_imprint("I-X", "LX", x_segments, 12)
        self.archive.record_imprint("I-Y", "LY", y_segments, 12)

    def _plan(self, limit: int, locked: bool = False):
        nodes = [
            dict(doc, customs_locked=(locked and doc["node_id"] == "CAB"))
            for doc in CABINET_TREE
        ]
        root, _ = build_tree(nodes)
        facts = classify_leaves(root, self.archive, frozenset({"A"}))
        return root, facts, plan_dispositions(root, facts, COSTS, limit)

    def test_clear_prints_open_cabinet_detain_x_release_y(self) -> None:
        self._issue_and_read(["11"], ["10"])
        _, _, plan = self._plan(limit=1)
        self.assertEqual(plan["total_work_hours"], 3)
        actions = [(a["action"], a["node_id"]) for a in plan["actions"]]
        self.assertIn(("unseal", "CAB"), actions)
        self.assertIn(("detain_leaf", "X"), actions)
        self.assertIn(("release", "Y"), actions)
        # no proven-clear box is co-returned in this plan
        self.assertEqual(plan["co_returned_clear"], 0)

    def test_worn_last_cells_make_both_suspect_so_seal_the_cabinet(self) -> None:
        self._issue_and_read(["1?"], ["1?"])
        _, _, plan = self._plan(limit=1)
        self.assertEqual(plan["total_work_hours"], 4)
        self.assertEqual(
            [a["action"] for a in plan["actions"]], ["seal_return"]
        )

    def test_customs_locked_cabinet_must_be_sealed_even_with_clear_reads(self) -> None:
        self._issue_and_read(["11"], ["10"])
        _, _, plan = self._plan(limit=1, locked=True)
        self.assertEqual(plan["total_work_hours"], 4)
        self.assertEqual(
            [a["action"] for a in plan["actions"]], ["seal_return"]
        )

    def test_zero_co_return_limit_still_finds_a_plan_when_opening_is_legal(self) -> None:
        self._issue_and_read(["11"], ["10"])
        _, _, plan = self._plan(limit=0)
        self.assertEqual(plan["total_work_hours"], 3)
        self.assertLessEqual(plan["co_returned_clear"], 0)

    def test_zero_co_return_limit_blocks_a_locked_cabinet_with_clear_collateral(self) -> None:
        # X is suspect, Y is proven clear but they share a customs-locked
        # cabinet: the only legal move is a sealed return that keeps one clear
        # box as collateral. A contract limit of zero makes that infeasible.
        self._issue_and_read(["1?"], ["10"])
        nodes = [
            dict(doc, customs_locked=(doc["node_id"] == "CAB"))
            for doc in CABINET_TREE
        ]
        root, _ = build_tree(nodes)
        facts = classify_leaves(root, self.archive, frozenset({"A"}))

        allowed = plan_dispositions(root, facts, COSTS, 1)
        self.assertEqual(allowed["total_work_hours"], 4)
        self.assertEqual(allowed["co_returned_clear"], 1)

        with self.assertRaises(ValidationError):
            plan_dispositions(root, facts, COSTS, 0)

    def test_sealed_return_charges_descendants_once(self) -> None:
        self._issue_and_read(["1?"], ["1?"])
        _, _, plan = self._plan(limit=1)
        sealed = next(a for a in plan["actions"] if a["action"] == "seal_return")
        self.assertEqual(sorted(sealed["labels"]), ["LX", "LY"])
        # exactly one charged action; leaves have no separate leaf charge
        self.assertEqual(sum(a["cost"] for a in plan["actions"]), 4)
        outcomes = {o["node_id"]: o["outcome"] for o in plan["leaf_outcomes"]}
        self.assertTrue(
            all("sealed return" in v for v in outcomes.values())
        )

    def test_illegal_or_unresolvable_box_stops_the_order(self) -> None:
        self._issue_and_read(["11"], ["10"])
        # add a leaf whose label was never legally issued
        nodes = CABINET_TREE + [
            {"node_id": "G", "kind": "leaf", "parent_id": "CAB",
             "label_id": "GHOST"}
        ]
        root, _ = build_tree(nodes)
        with self.assertRaises(PlanBlocked) as ctx:
            classify_leaves(root, self.archive, frozenset({"A"}))
        reasons = {b["label_id"]: b["reason"] for b in ctx.exception.blockers}
        self.assertIn("GHOST", reasons)

        # an unresolvable imprint likewise blocks rather than releasing:
        # file a new version of Y's read that no registered mold can match
        self.archive.record_imprint("I-Y", "LY", ["00"], 13)
        root2, _ = build_tree(CABINET_TREE)
        with self.assertRaises(PlanBlocked):
            classify_leaves(root2, self.archive, frozenset({"A"}))

    def test_three_level_tree_with_inner_cabinet(self) -> None:
        self.start_run("r1", 2, "W1", 2)
        self.seals.issue_label("LX", "X", "r1", 11)
        self.seals.issue_label("LY", "Y", "r1", 11)
        self.archive.register_mold("mA", "A", "00", 12)
        self.archive.register_mold("mB", "B", "01", 12)
        self.archive.record_imprint("I-X", "LX", ["11"], 12)
        self.archive.record_imprint("I-Y", "LY", ["10"], 12)
        nodes = [
            {"node_id": "OUT", "kind": "cabinet"},
            {"node_id": "IN", "kind": "cabinet", "parent_id": "OUT"},
            {"node_id": "X", "kind": "leaf", "parent_id": "IN", "label_id": "LX"},
            {"node_id": "Y", "kind": "leaf", "parent_id": "IN", "label_id": "LY"},
        ]
        costs = CostModel(
            sealed_return={"OUT": 5, "IN": 4},
            unseal={"OUT": 1, "IN": 1},
            leaf_charge=2,
        )
        root, _ = build_tree(nodes)
        facts = classify_leaves(root, self.archive, frozenset({"A"}))
        plan = plan_dispositions(root, facts, costs, 1)
        # unseal outer 1 + unseal inner 1 + detain X 2 + release Y 0 = 4
        self.assertEqual(plan["total_work_hours"], 4)


# ---------------------------------------------------------------------------
# workflow: cabinet-first, packet loss and reopen
# ---------------------------------------------------------------------------
class WorkflowRecoveryTests(SealStoreTestCase):
    def _ready_tree(self) -> list[dict]:
        self.start_run("r1", 2, "W1", 2)
        self.seals.issue_label("LX", "X", "r1", 11)
        self.seals.issue_label("LY", "Y", "r1", 11)
        self.archive.register_mold("mA", "A", "00", 12)
        self.archive.register_mold("mB", "B", "01", 12)
        self.archive.record_imprint("I-X", "LX", ["11"], 12)
        self.archive.record_imprint("I-Y", "LY", ["10"], 12)
        self.workflow.register_tree(CABINET_TREE)
        root, _ = build_tree(CABINET_TREE)
        facts = classify_leaves(root, self.archive, frozenset({"A"}))
        plan = plan_dispositions(root, facts, COSTS, 1)
        self.workflow.open_workflow("w1", 13)
        self.workflow.approve_plan("w1", plan)
        return CABINET_TREE

    def test_lost_break_packet_reopens_without_double_breaking_or_disposing(self) -> None:
        self._ready_tree()
        self.workflow.lock_cabinet("w1", "CAB", 13)
        receipt = self.workflow.break_seal(
            "w1", "CAB", 13, {"seal_serial": "S-001", "officer": "liu"}
        )
        self.assertFalse(receipt.get("replayed", False))

        # the scanner dies; the station closes and later reopens
        self.workflow.close_workflow("w1", 13)
        recovery = self.workflow.reopen_workflow("w1", 14)
        self.assertEqual(
            [s["node_id"] for s in recovery["broken_seals"]], ["CAB"]
        )
        pending = {(a["action"], a["node_id"]) for a in recovery["pending_actions"]}
        self.assertEqual(
            pending, {("detain_leaf", "X"), ("release", "Y")}
        )

        # the same broken seal is recognized and never broken twice
        replay = self.workflow.break_seal("w1", "CAB", 14, {"seal_serial": "S-001"})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["state"], "broken")

        for action in recovery["pending_actions"]:
            self.workflow.execute_action("w1", action, 14)

        # replaying a leaf disposition must not move the same box twice
        duplicate = self.workflow.execute_action(
            "w1", {"action": "release", "node_id": "Y"}, 14
        )
        self.assertTrue(duplicate["replayed"])

        rows = self.conn.execute(
            "SELECT label_id, action FROM leaf_dispositions WHERE workflow_id = 'w1'"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r["label_id"]: r["action"] for r in rows},
            {"LX": "detained", "LY": "released"},
        )
        final = self.workflow.recovery_state("w1")
        self.assertEqual(final["pending_actions"], [])
        self.workflow.finalize_workflow("w1", 15)
        self.assertEqual(self.workflow.get_workflow("w1")["state"], "finalized")

    def test_sealed_return_covers_descendants_and_rejects_later_leaf_action(self) -> None:
        self._ready_tree()
        self.workflow.lock_cabinet("w1", "CAB", 13)
        self.workflow.execute_action(
            "w1", {"action": "seal_return", "node_id": "CAB"}, 13
        )
        with self.assertRaises(Conflict):
            self.workflow.execute_action(
                "w1", {"action": "release", "node_id": "Y"}, 13
            )
        rows = self.conn.execute(
            "SELECT action, COUNT(*) AS n FROM leaf_dispositions "
            "WHERE workflow_id = 'w1' GROUP BY action"
        ).fetchall()
        self.assertEqual([(r["action"], r["n"]) for r in rows], [("detained", 2)])

    def test_box_first_merge_can_be_withdrawn_then_confirmed(self) -> None:
        self.start_run("r1", 2, "W1", 2)
        self.seals.issue_label("LX", "X", "r1", 11)
        self.seals.issue_label("LY", "Y", "r1", 11)
        self.workflow.register_tree(CABINET_TREE)
        self.workflow.open_workflow("wb", 12, mode="box_first")
        self.workflow.register_box_hold("wb", "LX", 12)
        self.workflow.register_box_hold("wb", "LY", 12)
        first_merge = self.workflow.merge_holds("wb", "CAB", 12)["merge_id"]
        withdrawn = self.workflow.withdraw_merge("wb", "CAB", 12)
        self.assertEqual(withdrawn["state"], "withdrawn")
        # holds return to the pending state and already-shipped facts survive
        states = {
            r["label_id"]: r["state"]
            for r in self.conn.execute(
                "SELECT h.label_id, h.state FROM box_holds h"
            )
        }
        self.assertTrue(all(v == "held" for v in states.values()))

        second_merge = self.workflow.merge_holds("wb", "CAB", 13)["merge_id"]
        self.assertNotEqual(first_merge, second_merge)
        sealed = self.workflow.confirm_merge_sealed("wb", "CAB", 13)
        self.assertEqual(sealed["state"], "sealed_returned")
        with self.assertRaises(Conflict):
            self.workflow.withdraw_merge("wb", "CAB", 13)


class ScaleLimitTests(SealStoreTestCase):
    def test_imprint_cannot_exceed_twenty_cells(self) -> None:
        from seals.matching import validate_imprint
        with self.assertRaises(ValueError):
            validate_imprint(
                Imprint(("1" * 21,), frozenset(), HOLE_PRESENT)
            )
        # exactly 20 is accepted structurally
        validate_imprint(Imprint(("1" * 20,), frozenset(), HOLE_PRESENT))

    def test_mold_cannot_exceed_forty_teeth(self) -> None:
        with self.assertRaises(ValidationError):
            self.archive.register_mold("big", "B", "0" * 41, 12)
        mold = self.archive.register_mold("ok", "OK", "0" * 40, 12)
        self.assertEqual(mold["tooth_count"], 40)

    def test_tree_cannot_exceed_three_levels(self) -> None:
        nodes = [
            {"node_id": "c0", "kind": "cabinet"},
            {"node_id": "c1", "kind": "cabinet", "parent_id": "c0"},
            {"node_id": "c2", "kind": "cabinet", "parent_id": "c1"},
            {"node_id": "c3", "kind": "cabinet", "parent_id": "c2"},
        ]
        with self.assertRaises(ValidationError):
            build_tree(nodes)

    def test_plan_carries_two_named_trucks(self) -> None:
        self.start_run("r1", 1, "W1", 1, start_day=11)
        self.seals.issue_label("LX", "X", "r1", 11)
        self.archive.register_mold("mA", "A", "00", 12)
        self.archive.record_imprint("I-X", "LX", ["11"], 12)
        nodes = [
            {"node_id": "CAB", "kind": "cabinet"},
            {"node_id": "X", "kind": "leaf", "parent_id": "CAB", "label_id": "LX"},
        ]
        root, _ = build_tree(nodes)
        facts = classify_leaves(root, self.archive, frozenset({"A"}))
        costs = CostModel(
            sealed_return={"CAB": 4}, unseal={"CAB": 1}, leaf_charge=2
        )
        plan = plan_dispositions(
            root, facts, costs, 1, trucks=("truck-alpha", "truck-beta")
        )
        self.assertEqual(plan["trucks"], ["truck-alpha", "truck-beta"])


if __name__ == "__main__":
    unittest.main()
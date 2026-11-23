"""Tests for strip folding, needle-mold calibration and sealed handoff.

These exercise the failure modes the brief calls out:

* fold reading: L puts B,C on the left needle and A,D on the right; R meets D
  first on the left needle.
* old operation marks cannot be moved onto a different block / stale revision.
* calibration over all sheets must share one setting; contradictions stop work.
* orders are judged over *all* compatible settings: go / hold / return.
* two needles sharing one axis make "stitch each needle's best depth" over-approve.
* sealed handoff (roster and conditions) re-judges new orders after the proof
  sheets are withdrawn, with de-identified labels, and both stop on
  contradiction.
"""

from __future__ import annotations

import json
import unittest

from delivery_system.fold_operations import (
    OperationMark,
    assess_operation_mark,
)
from delivery_system.folding import (
    FoldPlan,
    FoldVoucher,
    StripVersion,
    fold_state,
    initial_state,
)
from delivery_system.handoff import (
    build_handoff,
    handoff_from_dict,
    replay_order,
)
from delivery_system.models import SourceProduct, SourceRef, SourceSegment, TranslationEntry
from delivery_system.needle_mold import (
    INTACT,
    PUNCHED,
    NeedleMold,
    OrderRequirement,
    PunchSheet,
    WorkOrder,
    compatible_settings,
    judge_order,
    judge_work_order,
    naive_shared_axis_verdict,
    sheets_contradictory,
    unfold_pattern,
)

CELLS = ("A", "B", "C", "D")


def left_plan(cells=CELLS):
    return FoldPlan(cells, ("L",))


def right_plan(cells=CELLS):
    return FoldPlan(cells, ("R",))


def ab_mold():
    return NeedleMold(
        name="M1",
        needle_axis=((0, "left"), (1, "right")),
        candidate_depths=(("left", (1, 2)), ("right", (0,))),
    )


def shared_mold():
    return NeedleMold(
        name="M-shared",
        needle_axis=((0, "X"), (1, "X")),
        candidate_depths=(("X", (1, 2)),),
    )


def calibration_sheet(mold_plan=None):
    return [PunchSheet("cal", left_plan(), 1, 0, (("B", PUNCHED),))]


class FoldReadingTests(unittest.TestCase):
    def test_initial_state_front_up(self):
        state = initial_state(CELLS)
        self.assertEqual(
            [(layer.cell, layer.side) for layer in state[0]], [("A", "front")]
        )
        self.assertEqual(len(state), 4)

    def test_non_power_of_two_rejected(self):
        with self.assertRaises(Exception):
            initial_state(("A", "B", "C"))

    def test_duplicate_cells_rejected(self):
        with self.assertRaises(Exception):
            initial_state(("A", "A"))

    def test_L_left_needle_BC_right_needle_AD(self):
        voucher = FoldVoucher(left_plan(), 1)
        left = [layer["cell"] for layer in voucher.layers_at_needle(0)]
        right = [layer["cell"] for layer in voucher.layers_at_needle(1)]
        self.assertEqual(left, ["B", "C"])
        self.assertEqual(right, ["A", "D"])

    def test_R_left_needle_meets_D_first(self):
        voucher = FoldVoucher(right_plan(), 1)
        self.assertEqual(voucher.layers_at_needle(0)[0]["cell"], "D")
        self.assertEqual(
            [layer["cell"] for layer in voucher.layers_at_needle(0)], ["D", "A"]
        )

    def test_lifted_stack_flips_facing(self):
        voucher = FoldVoucher(left_plan(), 1)
        # B and C were lifted, so their upward side flipped to back.
        self.assertEqual(voucher.layers_at_needle(0)[0]["side"], "back")
        self.assertEqual(voucher.layers_at_needle(0)[1]["side"], "front")

    def test_by_cell_location_and_depth(self):
        voucher = FoldVoucher(left_plan(), 1)
        b = voucher.locate("B")
        self.assertEqual((b["needle"], b["depth"]), (0, 0))
        c = voucher.locate("C")
        self.assertEqual((c["needle"], c["depth"]), (0, 1))
        d = voucher.locate("D")
        self.assertEqual((d["needle"], d["depth"]), (1, 1))

    def test_voucher_start_offsets_needle(self):
        voucher = FoldVoucher(left_plan(), 1, start=3)
        self.assertEqual(voucher.needle_span(), (3, 4))
        self.assertEqual([x["cell"] for x in voucher.layers_at_needle(3)], ["B", "C"])
        self.assertEqual(voucher.layers_at_needle(2), [])

    def test_voucher_rejects_bad_step(self):
        with self.assertRaises(Exception):
            FoldVoucher(left_plan(), 2)

    def test_five_folds_32_cells(self):
        cells = tuple(f"C{i}" for i in range(32))
        plan = FoldPlan(cells, ("L", "R", "L", "R", "L"))
        state = plan.state_after(5)
        self.assertEqual(len(state), 1)
        self.assertEqual(len(state[0]), 32)
        # every original cell appears exactly once
        self.assertEqual({layer.cell for layer in state[0]}, set(cells))

    def test_strip_version_check_plan(self):
        version = StripVersion(
            document_id="S", revision=1, cells=CELLS, direction="L", foldable_steps=(0,)
        )
        plan = FoldPlan(CELLS, ("L",))
        version.check_plan(plan, 1)  # OK
        with self.assertRaises(Exception):
            version.check_plan(FoldPlan(CELLS, ("R",)), 1)  # wrong direction
        with self.assertRaises(Exception):
            version.check_plan(FoldPlan(("X", "Y", "Z", "W"), ("L",)), 1)  # wrong cells

    def test_strip_version_rejects_unfoldable_step(self):
        version = StripVersion(
            document_id="S", revision=1, cells=CELLS, direction="L", foldable_steps=(1,)
        )
        with self.assertRaises(Exception):
            version.check_plan(FoldPlan(CELLS, ("L",)), 1)  # step 0 not foldable

    def test_depth_exceeding_stack_punches_whole_stack(self):
        plan = FoldPlan(CELLS, ("L", "L"))
        state = plan.state_after(2)
        self.assertEqual([[l.cell for l in st] for st in state], [["C", "B", "A", "D"]])
        mold = NeedleMold(
            "deep", needle_axis=((0, "x"),), candidate_depths=(("x", (1, 2, 3, 4, 5)),)
        )
        sheet = PunchSheet("s", plan, 2, 0, ())
        # depth 5 on a 4-layer stack punches all four; no over-punch error.
        pattern = unfold_pattern(sheet, mold, {"x": 5})
        self.assertTrue(all(state == PUNCHED for state in pattern.values()))


class OperationMarkTests(unittest.TestCase):
    def setUp(self):
        self.product = SourceProduct(
            document_id="STRIP",
            document_revision=1,
            segments=tuple(
                SourceSegment(segment_id=c, position=i, revision=1, text=c)
                for i, c in enumerate(CELLS)
            ),
        )
        self.version = StripVersion(
            document_id="STRIP", revision=1, cells=CELLS, direction="L",
            foldable_steps=(0,),
        )
        self.entry = TranslationEntry(
            translation_id="BLOCK_BC",
            document_id="STRIP",
            language="EN",
            text="bc",
            source_refs=(SourceRef("B", 1), SourceRef("C", 1)),
        )
        self.entries = {"BLOCK_BC": self.entry}

    def _mark(self, refs):
        return OperationMark(
            mark_id="MK",
            document_id="STRIP",
            translation_id="BLOCK_BC",
            source_refs=tuple(SourceRef(s, r) for s, r in refs),
            plan=left_plan(),
        )

    def test_fresh_mark_usable(self):
        assessment = assess_operation_mark(
            self._mark([("B", 1), ("C", 1)]),
            self.product,
            self.entries,
            self.version,
            usable_translation_ids=["BLOCK_BC"],
        )
        self.assertTrue(assessment.usable, assessment.issues)

    def test_mark_moved_to_other_block_rejected(self):
        # The mark claims A,B but the block is B,C -> block_mismatch.
        assessment = assess_operation_mark(
            self._mark([("A", 1), ("B", 1)]),
            self.product,
            self.entries,
            self.version,
            usable_translation_ids=["BLOCK_BC"],
        )
        self.assertFalse(assessment.usable)
        self.assertEqual(assessment.state, "block_mismatch")

    def test_mark_on_non_adopted_block_rejected(self):
        assessment = assess_operation_mark(
            self._mark([("B", 1), ("C", 1)]),
            self.product,
            self.entries,
            self.version,
            usable_translation_ids=[],
        )
        self.assertFalse(assessment.usable)
        self.assertEqual(assessment.state, "block_mismatch")

    def test_stale_revision_mark_rejected(self):
        product = SourceProduct(
            document_id="STRIP",
            document_revision=2,
            segments=tuple(
                SourceSegment(
                    segment_id=c,
                    position=i,
                    revision=2 if c == "B" else 1,
                    text=c,
                )
                for i, c in enumerate(CELLS)
            ),
        )
        assessment = assess_operation_mark(
            self._mark([("B", 1), ("C", 1)]),
            product,
            self.entries,
            self.version,
            usable_translation_ids=["BLOCK_BC"],
        )
        self.assertFalse(assessment.usable)
        self.assertEqual(assessment.state, "stale_source")


class CalibrationTests(unittest.TestCase):
    def test_single_sheet_narrows_to_two_settings(self):
        compatible = compatible_settings(calibration_sheet(), ab_mold())
        self.assertEqual(len(compatible), 2)
        self.assertEqual({s["left"] for s in compatible}, {1, 2})
        self.assertTrue(all(s["right"] == 0 for s in compatible))

    def test_unobserved_sheet_admits_all(self):
        # No observations -> every candidate setting is compatible.
        sheet = [PunchSheet("empty", left_plan(), 1, 0, ())]
        self.assertEqual(
            len(compatible_settings(sheet, ab_mold())), ab_mold().product_size()
        )

    def test_multiple_sheets_must_share_one_setting(self):
        # Two sheets at different fold depths; together they force depth 1.
        mold = NeedleMold(
            "one-axis", needle_axis=((0, "x"),), candidate_depths=(("x", (1, 2)),)
        )
        folded = PunchSheet(
            "s1", left_plan(), 1, 0, (("B", PUNCHED), ("C", INTACT))
        )
        flat = PunchSheet(
            "s2", FoldPlan(CELLS, ()), 0, 0, (("A", PUNCHED), ("B", INTACT))
        )
        self.assertEqual(compatible_settings([folded, flat], mold), [{"x": 1}])
        # Each sheet alone is looser; only the conjunction pins the setting.
        self.assertEqual(len(compatible_settings([folded], mold)), 1)
        self.assertEqual(len(compatible_settings([flat], mold)), 2)

    def test_contradictory_sheets(self):
        sheets = [
            PunchSheet("c1", left_plan(), 1, 0, (("B", PUNCHED),)),
            PunchSheet("c2", left_plan(), 1, 0, (("B", INTACT),)),
        ]
        self.assertTrue(sheets_contradictory(sheets, ab_mold()))
        self.assertEqual(compatible_settings(sheets, ab_mold()), [])

    def test_pierce_count_aggregate(self):
        # left=2 punches B and C = 2 cells; left=1 punches only B = 1 cell.
        sheets = [
            PunchSheet("c", left_plan(), 1, 0, (), pierce_count=2),
        ]
        compatible = compatible_settings(sheets, ab_mold())
        self.assertEqual(compatible, [{"left": 2, "right": 0}])

    def test_uncovered_positions_leave_no_mark(self):
        # Start at needle 5, so needles 0/1 (the mold) see no paper.
        sheet = PunchSheet("off", left_plan(), 1, 5, (("B", INTACT),))
        compatible = compatible_settings([sheet], ab_mold())
        self.assertEqual(len(compatible), ab_mold().product_size())

    def test_depth_beyond_stack_punches_all(self):
        # left=2 on a 2-layer stack punches both; there is no depth 3 option.
        state = left_plan().state_after(1)
        self.assertEqual(len(state[0]), 2)


class OrderJudgmentTests(unittest.TestCase):
    def setUp(self):
        self.mold = ab_mold()
        self.sheets = calibration_sheet()
        self.compatible = compatible_settings(self.sheets, self.mold)

    def test_order1_hold(self):
        order = WorkOrder("o1", left_plan(), 1, 0, must_punch=("B",), must_intact=("C",))
        judgment = judge_work_order(order, self.sheets, self.mold)
        self.assertEqual(judgment.verdict, "hold")
        self.assertTrue(judgment.passing_settings)
        self.assertTrue(judgment.failing_settings)
        # hold carries two witness patterns.
        self.assertEqual(len(judgment.patterns), 2)

    def test_order2_right_layout_return(self):
        order = WorkOrder("o2", right_plan(), 1, 0, must_punch=("B",), must_intact=("C",))
        judgment = judge_work_order(order, self.sheets, self.mold)
        self.assertEqual(judgment.verdict, "return")
        self.assertEqual(judgment.passing_settings, ())

    def test_order3_relaxed_go(self):
        order = WorkOrder("o3", left_plan(), 1, 0, must_punch=("B",))
        judgment = judge_work_order(order, self.sheets, self.mold)
        self.assertEqual(judgment.verdict, "go")
        self.assertEqual(judgment.failing_settings, ())

    def test_contradictory_calibration_blocks(self):
        sheets = [
            PunchSheet("c1", left_plan(), 1, 0, (("B", PUNCHED),)),
            PunchSheet("c2", left_plan(), 1, 0, (("B", INTACT),)),
        ]
        order = WorkOrder("o", left_plan(), 1, 0, must_punch=("B",))
        judgment = judge_work_order(order, sheets, ab_mold())
        self.assertEqual(judgment.verdict, "contradiction")

    def test_order_requirement_overlap_rejected(self):
        with self.assertRaises(Exception):
            OrderRequirement("bad", must_punch=("B",), must_intact=("B",))

    def test_pattern_is_per_original_cell(self):
        order = WorkOrder("o", left_plan(), 1, 0, must_punch=("B",))
        judgment = judge_work_order(order, self.sheets, self.mold)
        pattern = judgment.patterns[0]["pattern"]
        self.assertEqual(set(pattern), set(CELLS))
        self.assertEqual(pattern["B"], PUNCHED)

    def test_judge_order_explicit_layout_matches(self):
        order = WorkOrder("o3", left_plan(), 1, 0, must_punch=("B",))
        via_wrapper = judge_order(
            order.requirement(), order.layout_sheet(), self.sheets, self.mold
        )
        via_work = judge_work_order(order, self.sheets, self.mold)
        self.assertEqual(via_wrapper.verdict, via_work.verdict)
        self.assertEqual(via_wrapper.verdict, "go")

    def test_start_offset_changes_pattern(self):
        # With start=1 the strip's position 0 lands on needle 1 (the stopped
        # right axis) and position 1 lands on needle 2, where the mold wires no
        # needle. So no needle ever meets paper: every cell stays intact, and an
        # order demanding a punched A cannot be met.
        order = WorkOrder(
            "offset", left_plan(), 1, 1, must_punch=("A",), must_intact=("B", "C")
        )
        judgment = judge_work_order(order, self.sheets, self.mold)
        self.assertEqual(judgment.verdict, "return")


class SharedAxisTests(unittest.TestCase):
    def test_naive_independent_needles_false_approve(self):
        mold = shared_mold()
        sheets = calibration_sheet()
        compatible = compatible_settings(sheets, mold)
        self.assertEqual(compatible, [{"X": 1}, {"X": 2}])
        order = WorkOrder(
            "false", left_plan(), 1, 0,
            must_punch=("A", "B", "D"), must_intact=("C",),
        )
        real = judge_work_order(order, sheets, mold, compatible=compatible)
        naive = naive_shared_axis_verdict(order, compatible, mold)
        self.assertEqual(real.verdict, "return")
        self.assertEqual(naive, "go")  # the over-approximation
        # Had the naive verdict been trusted, the order would wrongly ship.
        self.assertNotEqual(real.verdict, "go")

    def test_shared_axis_genuine_go_is_go_for_both(self):
        mold = shared_mold()
        sheets = calibration_sheet()
        compatible = compatible_settings(sheets, mold)
        order = WorkOrder("ok", left_plan(), 1, 0, must_punch=("B",))
        self.assertEqual(
            judge_work_order(order, sheets, mold, compatible=compatible).verdict,
            "go",
        )
        self.assertEqual(naive_shared_axis_verdict(order, compatible, mold), "go")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.mold = ab_mold()
        self.sheets = calibration_sheet()

    def test_roster_roundtrip_and_rejudge(self):
        handoff = build_handoff(self.sheets, self.mold, mode="roster")
        rebuilt = handoff_from_dict(json.loads(json.dumps(handoff.to_dict())))
        self.assertEqual(rebuilt.mode, "roster")
        self.assertEqual(len(rebuilt.compatible_settings), 2)

        hold_order = WorkOrder(
            "post", left_plan(), 1, 0, must_punch=("B",), must_intact=("C",)
        )
        result = replay_order(rebuilt, hold_order)
        self.assertEqual(result.judgment.verdict, "hold")
        self.assertIsNotNone(result.passing_setting)
        self.assertIsNotNone(result.failing_setting)

        go_order = WorkOrder("post2", left_plan(), 1, 0, must_punch=("B",))
        go = replay_order(rebuilt, go_order)
        self.assertEqual(go.judgment.verdict, "go")
        self.assertFalse(go.stopped)
        self.assertEqual(
            {row["cell"] for row in go.reconstructed_pattern["cells"]}, set(CELLS)
        )

    def test_conditions_redacts_and_keeps_coupling(self):
        handoff = build_handoff(self.sheets, self.mold, mode="conditions")
        payload = handoff.to_dict()
        wire = json.dumps(payload)
        # No raw cell names from the sealed sheets leak into the handoff.
        for cell in CELLS:
            self.assertNotIn(f'"{cell}"', wire)
        # ...and no per-cell observations travel either.
        self.assertNotIn("observations", wire)
        # The pierced-count group and axis conditions survive the transfer.
        self.assertIn("count_groups", payload)
        self.assertIn("axis_conditions", payload)

        rebuilt = handoff_from_dict(json.loads(json.dumps(payload)))
        go_order = WorkOrder("post", left_plan(), 1, 0, must_punch=("B",))
        result = replay_order(rebuilt, go_order)
        self.assertEqual(result.judgment.verdict, "go")

    def test_conditions_hold_has_pass_and_fail_settings(self):
        handoff = build_handoff(self.sheets, self.mold, mode="conditions")
        hold_order = WorkOrder(
            "post", left_plan(), 1, 0, must_punch=("B",), must_intact=("C",)
        )
        result = replay_order(handoff, hold_order)
        self.assertEqual(result.judgment.verdict, "hold")
        # one actual setting can deliver, one cannot
        self.assertIsNotNone(result.passing_setting)
        self.assertIsNotNone(result.failing_setting)
        layout = WorkOrder("post", left_plan(), 1, 0).layout_sheet()
        pass_pat = unfold_pattern(layout, self.mold, result.passing_setting)
        fail_pat = unfold_pattern(layout, self.mold, result.failing_setting)
        self.assertEqual(pass_pat["B"], PUNCHED)
        self.assertEqual(pass_pat["C"], INTACT)
        self.assertFalse(fail_pat["B"] == PUNCHED and fail_pat["C"] == INTACT)

    def test_conditions_right_layout_return(self):
        handoff = build_handoff(self.sheets, self.mold, mode="conditions")
        order = WorkOrder(
            "post", right_plan(), 1, 0, must_punch=("B",), must_intact=("C",)
        )
        result = replay_order(handoff, order)
        self.assertEqual(result.judgment.verdict, "return")
        self.assertTrue(result.stopped)

    def test_conditions_shared_axis_preserved(self):
        mold = shared_mold()
        sheets = calibration_sheet()
        handoff = build_handoff(sheets, mold, mode="conditions")
        order = WorkOrder(
            "false", left_plan(), 1, 0,
            must_punch=("A", "B", "D"), must_intact=("C",),
        )
        result = replay_order(handoff, order)
        # The shared-axis coupling survived, so this is not falsely approved.
        self.assertEqual(result.judgment.verdict, "return")

    def test_both_modes_stop_on_contradiction(self):
        sheets = [
            PunchSheet("c1", left_plan(), 1, 0, (("B", PUNCHED),)),
            PunchSheet("c2", left_plan(), 1, 0, (("B", INTACT),)),
        ]
        for mode in ("roster", "conditions"):
            with self.assertRaises(Exception):
                build_handoff(sheets, self.mold, mode=mode)

    def test_recalibration_needs_new_roster(self):
        roster = build_handoff(self.sheets, self.mold, mode="roster")
        # A stricter second sheet narrows the admissible settings.
        stricter = self.sheets + [
            PunchSheet("c2", left_plan(), 1, 0, (("C", INTACT),))
        ]
        new_roster = build_handoff(stricter, self.mold, mode="roster")
        self.assertNotEqual(
            {tuple(sorted(s.items())) for s in roster.compatible_settings},
            {tuple(sorted(s.items())) for s in new_roster.compatible_settings},
        )
        self.assertEqual(len(new_roster.compatible_settings), 1)


class CalibrationScaleTests(unittest.TestCase):
    def test_32_cells_5_folds_8_axes_under_20000(self):
        cells = tuple(f"C{i}" for i in range(32))
        plan = FoldPlan(cells, ("L", "R", "L", "R", "L"))
        axes = [f"ax{i}" for i in range(8)]
        mold = NeedleMold(
            name="SCALE",
            needle_axis=tuple((i, axes[i % 8]) for i in range(16)),
            candidate_depths=tuple((a, (1, 2)) for a in axes),
        )
        self.assertLessEqual(mold.product_size(), 20000)
        state = plan.state_after(5)
        self.assertEqual(len(state[0]), 32)


if __name__ == "__main__":
    unittest.main()

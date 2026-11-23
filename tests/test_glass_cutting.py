"""Tests for resumable guillotine cutting."""

import unittest

from glass.cutting import (
    Defect,
    Ledger,
    PaneSpec,
    Piece,
    PlanError,
    check_placements_within_pieces,
    guillotine_order,
    plan_cuts,
)


def sheet(pid, w, h, grain="vertical", color="clear", defects=(), x=0, y=0):
    return Piece(pid, pid, x, y, w, h, grain, color, tuple(defects))


def spec(demand_id, sizes, grain="vertical", color="clear"):
    return PaneSpec(demand_id, demand_id, color, grain, list(sizes))


class BasicCutTests(unittest.TestCase):
    def test_two_vertical_panes_from_one_cut(self):
        plan = plan_cuts(
            [spec("L", [(1, 2)]), spec("R", [(1, 2)])],
            Ledger([sheet("S", 2, 2)], 1),
        )
        self.assertEqual(len(plan.cuts), 1)
        self.assertEqual(plan.cuts[0].axis, "vertical")
        self.assertEqual(plan.cuts[0].at, 1)
        self.assertEqual(plan.remaining_cuts, 0)
        self.assertEqual(
            sorted(p.crop for p in plan.placements),
            [(0, 0, 1, 2), (1, 0, 1, 2)],
        )

    def test_needs_two_cuts_for_two_by_two(self):
        # A 2x2 into four 1x1 panes takes at least 3 cuts (1 + 1 + 1).
        plan = plan_cuts(
            [spec(f"P{i}", [(1, 1)]) for i in range(4)],
            Ledger([sheet("S", 2, 2)], 3),
        )
        self.assertEqual(len(plan.cuts), 3)
        self.assertEqual(plan.remaining_cuts, 0)

    def test_cut_budget_too_small_refused(self):
        with self.assertRaises(PlanError):
            plan_cuts(
                [spec(f"P{i}", [(1, 1)]) for i in range(4)],
                Ledger([sheet("S", 2, 2)], 2),
            )

    def test_defect_inside_pane_forbidden(self):
        # A 2x2 sheet with a defect in the bottom-right quadrant; a 2x2 pane
        # cannot contain it.
        bad = sheet("S", 2, 2, defects=[Defect(1, 1, 1, 1)])
        with self.assertRaises(PlanError):
            plan_cuts([spec("P", [(2, 2)])], Ledger([bad], 0))
        # A 1x1 pane in a clear corner is fine, and costs two trims.
        plan = plan_cuts([spec("P", [(1, 1)])], Ledger([bad], 2))
        crop = plan.placements[0].crop
        self.assertFalse(crop[0] >= 1 and crop[1] >= 1)
        self.assertEqual(len(plan.cuts), 2)

    def test_color_must_match(self):
        with self.assertRaises(PlanError):
            plan_cuts(
                [spec("P", [(1, 1)], color="gray")],
                Ledger([sheet("S", 1, 1, color="clear")], 0),
            )


class GrainTests(unittest.TestCase):
    def test_vertical_grain_turn_is_identity(self):
        plan = plan_cuts(
            [spec("P", [(1, 2)], grain="vertical")],
            Ledger([sheet("S", 2, 2, grain="vertical")], 1),
        )
        self.assertEqual(plan.placements[0].turn, 0)

    def test_horizontal_grain_needs_turn(self):
        # A vertical-grain piece turned a quarter turn serves a horizontal slot.
        plan = plan_cuts(
            [spec("P", [(2, 1)], grain="horizontal")],
            Ledger([sheet("S", 1, 2, grain="vertical")], 0),
        )
        self.assertEqual(plan.placements[0].turn, 1)
        self.assertEqual(plan.placements[0].size, (2, 1))

    def test_grain_axis_must_match_physically(self):
        with self.assertRaises(PlanError):
            plan_cuts(
                [spec("P", [(2, 1)], grain="horizontal")],
                Ledger([sheet("S", 2, 1, grain="vertical")], 0),
            )


class HandoffTests(unittest.TestCase):
    def test_two_horizontal_remnants_cannot_make_tall_strip(self):
        # The sheet was already cut horizontally into two 2x1 halves; the tall
        # 1x2 vertical pane cannot be reassembled from the halves.
        pieces = [sheet("H1", 2, 1, x=0, y=0), sheet("H2", 2, 1, x=0, y=1)]
        with self.assertRaises(PlanError):
            plan_cuts([spec("P", [(1, 2)])], Ledger(pieces, 1))

    def test_same_area_but_wrong_shape_refused(self):
        # A 2x1 remnant has the same area as a 1x2 pane but wrong shape.
        with self.assertRaises(PlanError):
            plan_cuts(
                [spec("P", [(1, 2)])],
                Ledger([sheet("S", 2, 1)], 0),
            )

    def test_resume_from_vertical_halves(self):
        # Two 1x2 vertical halves can still each yield a 1x2 pane.
        pieces = [sheet("A", 1, 2, x=0, y=0), sheet("B", 1, 2, x=1, y=0)]
        plan = plan_cuts(
            [spec("L", [(1, 2)]), spec("R", [(1, 2)])], Ledger(pieces, 0)
        )
        self.assertEqual(plan.cuts, [])
        self.assertEqual(sorted(p.piece for p in plan.placements), ["A", "B"])

    def test_fracture_check_flags_spanning_crop(self):
        from glass.cutting import Placement

        pieces = [sheet("H1", 2, 1, y=0), sheet("H2", 2, 1, y=1)]
        span = Placement("P", "S", "H1", (0, 0, 1, 2), (1, 2), 0, None)
        problems = check_placements_within_pieces([span], pieces)
        self.assertTrue(problems)

    def test_offcut_trim_costs_a_cut(self):
        # A 3x3 sheet, one 1x1 pane from a corner: two trims needed.
        plan = plan_cuts([spec("P", [(1, 1)])], Ledger([sheet("S", 3, 3)], 2))
        self.assertEqual(len(plan.cuts), 2)
        self.assertEqual(plan.remaining_cuts, 0)


class MultipleSizeTests(unittest.TestCase):
    def test_picks_a_legal_size_per_slot(self):
        # Each slot can be 1x2 or 2x1; four 2x2 sheets, one pane each.
        plan = plan_cuts(
            [spec(f"P{i}", [(1, 2), (2, 1)]) for i in range(2)],
            Ledger([sheet("S", 2, 2)], 1),
        )
        self.assertEqual(len(plan.placements), 2)
        self.assertLessEqual(len(plan.cuts), 1)


class GuillotineOrderTests(unittest.TestCase):
    def test_grid_has_order(self):
        order = guillotine_order(
            [sheet("S", 2, 2)],
            [(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 1, 1), (1, 1, 1, 1)],
        )
        self.assertIsNotNone(order)
        self.assertEqual(len(order), 3)

    def test_pinwheel_has_no_order(self):
        pinwheel = [
            (0, 0, 2, 1),
            (2, 0, 1, 2),
            (1, 2, 2, 1),
            (0, 1, 1, 2),
            (1, 1, 1, 1),
        ]
        self.assertIsNone(
            guillotine_order([sheet("S", 3, 3)], pinwheel, max_depth=40)
        )


if __name__ == "__main__":
    unittest.main()

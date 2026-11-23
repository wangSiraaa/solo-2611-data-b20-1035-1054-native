"""Tests for the glazing fit engine and orientation helpers."""

import unittest

from glass import fit, geom


class OrientationTests(unittest.TestCase):
    def test_side_rotation_round_trip(self):
        for side in geom.SIDES:
            for turns in range(4):
                back = geom.bench_to_install_side(
                    geom.rotate_cw_side(side, turns), turns
                )
                self.assertEqual(back, side)

    def test_corner_rotation_round_trip(self):
        for corner in geom.CORNERS:
            for turns in range(4):
                back = geom.bench_to_install_corner(
                    geom.rotate_cw_corner(corner, turns), turns
                )
                self.assertEqual(back, corner)

    def test_quarter_turn_swaps_axis_and_dims(self):
        self.assertEqual(geom.bench_to_install_axis("vertical", 1), "horizontal")
        self.assertEqual(geom.bench_to_install_axis("vertical", 2), "vertical")
        self.assertEqual(geom.bench_dims_to_install(1, 2, 1), (2, 1))
        self.assertEqual(geom.bench_dims_to_install(1, 2, 2), (1, 2))

    def test_arrow_reversal_same_axis(self):
        # A 180-degree turn is the same physical grain direction.
        self.assertEqual(geom.bench_to_install_axis("vertical", 2), "vertical")


class NormalizeTests(unittest.TestCase):
    def test_opening_rotated_to_front(self):
        readings = fit.normalize_quantity(
            {
                "slot": "S",
                "install_position": "p",
                "item": "opening",
                "width": 1,
                "height": 2,
                "revision": 1,
                "rotation": 1,
            }
        )
        self.assertEqual(readings[0].payload, (2, 1))

    def test_lap_side_rotated(self):
        readings = fit.normalize_quantity(
            {
                "slot": "S",
                "install_position": "p",
                "item": "lap",
                "side": "top",
                "min": 1,
                "max": 1,
                "revision": 1,
                "rotation": 1,
            }
        )
        sides = {r.target for r in readings}
        self.assertEqual(sides, {"left"})

    def test_grain_rotated(self):
        readings = fit.normalize_quantity(
            {
                "slot": "S",
                "install_position": "p",
                "item": "grain",
                "axis": "vertical",
                "revision": 1,
                "rotation": 3,
            }
        )
        self.assertEqual(readings[0].payload, ("horizontal",))

    def test_bad_item(self):
        with self.assertRaises(fit.FitError):
            fit.normalize_quantity(
                {"slot": "S", "install_position": "p", "item": "nope", "revision": 1}
            )


class SelectTests(unittest.TestCase):
    def _lap(self, revision, side="top", value=1):
        return {
            "slot": "S",
            "install_position": "p",
            "item": "lap",
            "side": side,
            "min": value,
            "max": value,
            "revision": revision,
        }

    def test_highest_round_wins(self):
        chosen = fit.select_readings([self._lap(1, value=0), self._lap(2, value=1)])
        self.assertEqual(chosen[("S", "p", "min_lap", "top")].revision, 2)
        self.assertEqual(chosen[("S", "p", "min_lap", "top")].payload, (1,))

    def test_same_round_conflict_rejected(self):
        with self.assertRaises(fit.FitError):
            fit.select_readings([self._lap(1, value=0), self._lap(1, value=1)])

    def test_same_round_identical_collapses(self):
        chosen = fit.select_readings([self._lap(1, value=1), self._lap(1, value=1)])
        self.assertEqual(len(chosen), 2)  # min_lap + max_lap keys


def laps(opening, sides=None, corners=None, grain=None):
    """Build a reading map for one slot/position."""

    raws = [
        {
            "slot": "S",
            "install_position": "p",
            "item": "opening",
            "width": opening[0],
            "height": opening[1],
            "revision": 1,
        }
    ]
    for side, (lo, hi) in (sides or {}).items():
        raws.append(
            {
                "slot": "S",
                "install_position": "p",
                "item": "lap",
                "side": side,
                "min": lo,
                "max": hi,
                "revision": 1,
            }
        )
    for corner, value in (corners or {}).items():
        raws.append(
            {
                "slot": "S",
                "install_position": "p",
                "item": "corner",
                "corner": corner,
                "value": value,
                "revision": 1,
            }
        )
    if grain is not None:
        raws.append(
            {
                "slot": "S",
                "install_position": "p",
                "item": "grain",
                "axis": grain,
                "revision": 1,
            }
        )
    return fit.select_readings(raws)


class FitTests(unittest.TestCase):
    def test_single_exact_lap(self):
        req = fit.build_requirement(
            "S", "c", "p",
            laps((1, 1), {"top": (1, 1), "left": (0, 0), "right": (0, 0),
                          "bottom": (0, 0)}, grain="vertical"),
        )
        self.assertEqual(req.opening, (1, 1))
        self.assertEqual(req.grain, "vertical")
        self.assertEqual(req.maximal_sizes(), [
            {"width": 1, "height": 2,
             "laps": {"left": 0, "right": 0, "top": 1, "bottom": 0}}
        ])

    def test_corner_couples_width_and_height(self):
        # Left and top can each lap 0 or 1, but the TL baffle is 1, so 2x2 is
        # impossible while 1x2 and 2x1 are both legal.
        req = fit.build_requirement(
            "S", "c", "p",
            laps((1, 1), {"left": (0, 1), "top": (0, 1), "right": (0, 0),
                          "bottom": (0, 0)}, corners={"TL": 1}),
        )
        maximal = {(s["width"], s["height"]) for s in req.maximal_sizes()}
        self.assertIn((1, 2), maximal)
        self.assertIn((2, 1), maximal)
        self.assertNotIn((2, 2), maximal)
        self.assertFalse(req.explain(2, 2)["accepted"])
        self.assertTrue(req.explain(1, 2)["accepted"])
        self.assertTrue(req.explain(2, 1)["accepted"])

    def test_max_width_and_height_need_not_coincide(self):
        req = fit.build_requirement(
            "S", "c", "p",
            laps((1, 1), {"left": (0, 1), "top": (0, 1), "right": (0, 0),
                          "bottom": (0, 0)}, corners={"TL": 1}),
        )
        self.assertEqual(req.max_width(), 2)
        self.assertEqual(req.max_height(), 2)
        # Both maxima individually exist, yet 2x2 is not on any single pane.
        self.assertIsNone(req.feasible_laps(2, 2))

    def test_explain_reports_laps(self):
        req = fit.build_requirement(
            "S", "c", "p",
            laps((1, 1), {"left": (0, 1), "right": (0, 0), "top": (0, 0),
                          "bottom": (0, 0)}),
        )
        explained = req.explain(2, 1)
        self.assertTrue(explained["accepted"])
        self.assertEqual(explained["laps"],
                         {"left": 1, "right": 0, "top": 0, "bottom": 0})

    def test_missing_opening_is_error(self):
        readings = laps((1, 1), {"left": (0, 0), "right": (0, 0),
                                 "top": (0, 0), "bottom": (0, 0)})
        req = fit.build_requirement("S", "c", "p", readings)
        req.opening = None
        self.assertIn("missing clear-opening measurement", req.requirement_errors())

    def test_ungrained_slot_can_omit_grain(self):
        req = fit.build_requirement(
            "S", "c", "p",
            laps((1, 1), {"left": (0, 0), "right": (0, 0),
                          "top": (0, 0), "bottom": (0, 0)}),
        )
        self.assertIsNone(req.grain)
        self.assertEqual(req.requirement_errors(), [])

    def test_infeasible_when_window_too_large(self):
        req = fit.build_requirement(
            "S", "c", "p",
            laps((1, 1), {"left": (0, 0), "right": (0, 0),
                          "top": (0, 0), "bottom": (0, 0)}),
        )
        self.assertFalse(req.explain(3, 1)["accepted"])


if __name__ == "__main__":
    unittest.main()

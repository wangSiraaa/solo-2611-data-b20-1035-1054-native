"""Tests for the glazing store: resolution, revisions, signings, planning."""

import os
import tempfile
import unittest

from cs.store import CatalogError
from glass import fit
from glass.store import GlassError, GlazingStore


def fresh(testcase) -> GlazingStore:
    path = tempfile.mktemp(suffix=".db")
    testcase.addCleanup(lambda: [
        os.unlink(p) for p in (path, path + "-wal", path + "-shm")
        if os.path.exists(p)
    ])
    gs = GlazingStore.open(path)
    testcase.addCleanup(gs.close)
    gs.store.init_catalog(
        {
            "format": "classification-catalog/1",
            "revision": "G1",
            "nodes": [
                {"id": "ALL", "name": "全部"},
                {"id": "FAM", "name": "同族"},
                {"id": "SL", "name": "左槽"},
                {"id": "SR", "name": "右槽"},
            ],
            "edges": [["SL", "FAM"], ["SR", "FAM"], ["FAM", "ALL"]],
        }
    )
    gs.define_slot("SL", "gray", True, "left")
    gs.define_slot("SR", "clear", False, "right")
    return gs


def base_measurements(revision=1, right=0):
    return [
        {"slot": "SL", "install_position": "p", "item": "opening",
         "width": 1, "height": 1, "revision": revision},
        {"slot": "SL", "install_position": "p", "item": "lap",
         "side": "top", "min": 1, "max": 1, "revision": revision},
        {"slot": "SL", "install_position": "p", "item": "lap",
         "side": "left", "min": 0, "max": 0, "revision": revision},
        {"slot": "SL", "install_position": "p", "item": "lap",
         "side": "right", "min": right, "max": right, "revision": revision},
        {"slot": "SL", "install_position": "p", "item": "lap",
         "side": "bottom", "min": 0, "max": 0, "revision": revision},
        {"slot": "SL", "install_position": "p", "item": "grain",
         "axis": "vertical", "revision": revision},
    ]


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.gs = fresh(self)

    def test_single_groove_resolves(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements())
        result = self.gs.resolve_survey("q1")
        self.assertTrue(result.usable)
        self.assertEqual(result.slot, "SL")

    def test_zero_grooves_unusable(self):
        # ALL is not a slot; a ticket directly on ALL reaches no groove.
        self.gs.submit_quantity(
            "q2", "T2", 1, ["ALL"], base_measurements()
        )
        result = self.gs.resolve_survey("q2")
        self.assertFalse(result.usable)
        self.assertIsNone(result.slot)
        self.assertEqual(result.reachable, [])

    def test_multiple_grooves_unusable(self):
        # Filed under both grooves at once: reaches SL and SR, so unusable.
        self.gs.submit_quantity(
            "q3", "T3", 1, ["SL", "SR"], base_measurements()
        )
        result = self.gs.resolve_survey("q3")
        self.assertFalse(result.usable)
        self.assertEqual(result.reachable, ["SL", "SR"])

    def test_multi_path_single_groove_counts_once(self):
        # Add a second path from SL to FAM; still one groove, one contribution.
        self.gs.store.add_nodes([{"id": "X"}], request_id="add-x")
        self.gs.store.submit(
            {"operation": "catalog", "add_edges": [["SL", "X"], ["X", "FAM"]]},
            request_id="x-path",
        )
        self.gs.submit_quantity("q4", "T4", 1, ["SL"], base_measurements())
        result = self.gs.resolve_survey("q4")
        self.assertTrue(result.usable)
        self.assertEqual(result.slot, "SL")
        self.assertEqual(len(result.contributions["SL"]), 1)

    def test_unknown_class_rejected(self):
        with self.assertRaises(CatalogError):
            self.gs.submit_quantity(
                "q5", "T5", 1, ["NOPE"], base_measurements()
            )

    def test_ambiguous_survey_contributes_no_readings(self):
        # Filed under both grooves: no single groove, so its laps never apply.
        self.gs.submit_quantity(
            "amb", "TA", 1, ["SL", "SR"], base_measurements()
        )
        self.assertEqual(self.gs.reading_set(), {})

    def test_zero_reach_survey_contributes_no_readings(self):
        self.gs.submit_quantity("zero", "TZ", 1, ["ALL"], base_measurements())
        self.assertEqual(self.gs.reading_set(), {})

    def test_same_round_two_tickets_conflict_retake(self):
        # Two independent slips file to the same slot at the same round with
        # different top laps: the reading set is contradictory and must retake.
        self.gs.submit_quantity("a", "TA", 1, ["SL"], base_measurements(1))
        conflicting = [
            {"slot": "SL", "install_position": "p", "item": "lap",
             "side": "top", "min": 0, "max": 0, "revision": 1}
        ]
        self.gs.submit_quantity("b", "TB", 1, ["SL"], conflicting)
        with self.assertRaises(fit.FitError):
            self.gs.reading_set()


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.gs = fresh(self)

    def test_highest_round_wins(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements(1, right=0))
        self.gs.submit_quantity("q2", "T1", 2, ["SL"], base_measurements(2, right=1))
        reading = self.gs.reading_set()[("SL", "p", "max_lap", "right")]
        self.assertEqual(reading.payload, (1,))

    def test_same_round_different_readings_retake(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements(1, right=0))
        with self.assertRaises(GlassError):
            self.gs.submit_quantity(
                "q2", "T1", 1, ["SL"], base_measurements(1, right=1)
            )

    def test_stale_round_rejected(self):
        self.gs.submit_quantity("q1", "T1", 2, ["SL"], base_measurements(2))
        with self.assertRaises(GlassError):
            self.gs.submit_quantity("q2", "T1", 1, ["SL"], base_measurements(1))

    def test_withdraw_exposes_previous_round(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements(1, right=0))
        self.gs.submit_quantity("q2", "T1", 2, ["SL"], base_measurements(2, right=1))
        self.assertEqual(
            self.gs.reading_set()[("SL", "p", "max_lap", "right")].payload, (1,)
        )
        out = self.gs.withdraw_quantity("T1")
        self.assertEqual(out["exposed_revision"], 1)
        self.assertEqual(
            self.gs.reading_set()[("SL", "p", "max_lap", "right")].payload, (0,)
        )

    def test_round_is_selected_per_item_not_per_slip(self):
        # Round 1 records top=1 and right=0.  Round 2 re-measures only the right
        # lap; the older top reading stays in force.
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements(1, right=0))
        only_right = [
            {"slot": "SL", "install_position": "p", "item": "lap",
             "side": "right", "min": 1, "max": 1, "revision": 2}
        ]
        self.gs.submit_quantity("q2", "T1", 2, ["SL"], only_right)
        readings = self.gs.reading_set()
        self.assertEqual(readings[("SL", "p", "max_lap", "right")].payload, (1,))
        # The top lap was not re-measured, so it still reads round 1.
        self.assertEqual(readings[("SL", "p", "max_lap", "top")].payload, (1,))
        self.assertEqual(readings[("SL", "p", "max_lap", "top")].revision, 1)

    def test_withdrawn_survey_not_effective(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements(1))
        self.gs.withdraw_quantity("T1")
        self.assertFalse(self.gs.resolve_survey("q1").usable)

    def test_atomic_batch_visible_together(self):
        self.gs.submit_quantities(
            [
                {"survey_id": "a", "ticket_id": "TA", "revision": 1,
                 "direct_classes": ["SL"], "measurements": base_measurements(1, right=1)},
                {"survey_id": "b", "ticket_id": "TB", "revision": 1,
                 "direct_classes": ["SR"], "measurements": [
                     {"slot": "SR", "install_position": "p", "item": "opening",
                      "width": 2, "height": 1, "revision": 1}]},
            ]
        )
        self.assertIn(("SL", "p", "max_lap", "right"), self.gs.reading_set())
        self.assertIn(("SR", "p", "opening", ""), self.gs.reading_set())

    def test_atomic_batch_conflict_writes_nothing(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements(1, right=0))
        before = self.gs.glass_watermark()
        with self.assertRaises(GlassError):
            self.gs.submit_quantities(
                [
                    {"survey_id": "ok", "ticket_id": "TX", "revision": 1,
                     "direct_classes": ["SR"], "measurements": []},
                    {"survey_id": "bad", "ticket_id": "T1", "revision": 1,
                     "direct_classes": ["SL"], "measurements": base_measurements(1, right=1)},
                ]
            )
        # TX must not have been written, watermark unchanged.
        self.assertEqual(self.gs.glass_watermark(), before)
        self.assertNotIn(("SR", "p", "opening", ""), self.gs.reading_set())


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.gs = fresh(self)

    def test_sign_records_sizes_and_grain(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements())
        slip = self.gs.sign("S1", "SL", "p")
        self.assertEqual(slip["grain"], "vertical")
        self.assertEqual([(s["width"], s["height"]) for s in slip["sizes"]],
                         [(1, 2)])

    def test_signing_is_immutable_across_bead_change(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements())
        slip = self.gs.sign("S1", "SL", "p")
        old = [(s["width"], s["height"]) for s in slip["sizes"]]
        # Change the beads: right lap becomes 1, giving a 2x2 pane.
        self.gs.submit_quantity("q2", "T1", 2, ["SL"], base_measurements(2, right=1))
        new = self.gs.sign("S2", "SL", "p")
        self.assertEqual([(s["width"], s["height"]) for s in new["sizes"]], [(2, 2)])
        # The old slip still reads exactly as it did.
        reread = self.gs.get_signing("S1")
        self.assertEqual([(s["width"], s["height"]) for s in reread["sizes"]], old)

    def test_sign_requires_grain_for_grained_slot(self):
        measurements = [m for m in base_measurements() if m["item"] != "grain"]
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], measurements)
        with self.assertRaises(fit.FitError):
            self.gs.sign("S1", "SL", "p")

    def test_sign_ungrained_slot_without_grain(self):
        self.gs.submit_quantity(
            "q1", "T1", 1, ["SR"],
            [
                {"slot": "SR", "install_position": "p", "item": "opening",
                 "width": 1, "height": 1, "revision": 1},
                {"slot": "SR", "install_position": "p", "item": "lap",
                 "side": "left", "min": 0, "max": 0, "revision": 1},
                {"slot": "SR", "install_position": "p", "item": "lap",
                 "side": "right", "min": 0, "max": 0, "revision": 1},
                {"slot": "SR", "install_position": "p", "item": "lap",
                 "side": "top", "min": 0, "max": 0, "revision": 1},
                {"slot": "SR", "install_position": "p", "item": "lap",
                 "side": "bottom", "min": 0, "max": 0, "revision": 1},
            ],
        )
        slip = self.gs.sign("S1", "SR", "p")
        self.assertIsNone(slip["grain"])

    def test_explain_size(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements())
        self.gs.sign("S1", "SL", "p")
        explained = self.gs.explain_size("S1", 1, 2)
        self.assertTrue(explained["accepted"])
        refused = self.gs.explain_size("S1", 2, 2)
        self.assertFalse(refused["accepted"])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.gs = fresh(self)

    def _sign(self):
        self.gs.submit_quantity("q1", "T1", 1, ["SL"], base_measurements())
        return self.gs.sign("S1", "SL", "p")

    def test_plan_two_panes_one_cut(self):
        self._sign()
        self.gs.define_slot("SR", "gray", True)
        self.gs.submit_quantity(
            "q2", "T2", 1, ["SR"],
            [dict(m, slot="SR") for m in base_measurements()],
        )
        self.gs.sign("S2", "SR", "p")
        self.gs.register_stock(
            "B1",
            [{"id": "S", "width": 2, "height": 2, "grain": "vertical",
              "color": "gray"}],
            1,
        )
        plan = self.gs.plan("P1", "B1", ["S1", "S2"])
        self.assertEqual(len(plan["cuts"]), 1)
        self.assertEqual(plan["cuts_used"], 1)
        self.assertEqual(len(plan["placements"]), 2)
        self.assertEqual(plan["handoff"]["physical_fracture_check"], [])

    def test_one_pane_per_slot(self):
        # Two signings for the same slot must demand a single pane, not two.
        self._sign()
        self.gs.sign("S2", "SL", "p")  # same slot surveyed twice
        self.gs.register_stock(
            "B1",
            [{"id": "S", "width": 1, "height": 2, "grain": "vertical",
              "color": "gray"}],
            0,
        )
        plan = self.gs.plan("P1", "B1", ["S1", "S2"])
        self.assertEqual(len(plan["placements"]), 1)

    def test_plan_refuses_when_area_wrong(self):
        self._sign()
        self.gs.register_stock(
            "B1",
            [{"id": "S", "width": 2, "height": 1, "grain": "vertical",
              "color": "gray"}],
            0,
        )
        from glass.cutting import PlanError
        with self.assertRaises(PlanError):
            self.gs.plan("P1", "B1", ["S1"])

    def test_plan_is_stored_and_rereadable(self):
        self._sign()
        self.gs.register_stock(
            "B1",
            [{"id": "S", "width": 1, "height": 2, "grain": "vertical",
              "color": "gray"}],
            0,
        )
        plan = self.gs.plan("P1", "B1", ["S1"])
        self.assertEqual(self.gs.get_plan("P1"), plan)

    def test_resume_from_remnants(self):
        self._sign()
        self.gs.define_slot("SR", "gray", True)
        self.gs.submit_quantity(
            "q2", "T2", 1, ["SR"],
            [dict(m, slot="SR") for m in base_measurements()],
        )
        self.gs.sign("S2", "SR", "p")
        self.gs.register_stock(
            "B1",
            [{"id": "S", "width": 2, "height": 2, "grain": "vertical",
              "color": "gray"}],
            1,
        )
        self.gs.plan("P1", "B1", ["S1", "S2"])
        # Pretend S1 was delivered and only S2 remains; hand over the offcut.
        self.gs.register_stock(
            "B1R",
            [{"id": "OFF", "width": 1, "height": 2, "grain": "vertical",
              "color": "gray"}],
            0,
        )
        resumed = self.gs.resume("P1", "B1R", delivered=["S1"])
        self.assertEqual(resumed["remaining_signings"], ["S2"])
        self.assertEqual(len(resumed["placements"]), 1)
        self.assertEqual(resumed["fracture_check"], [])

    def test_resume_refuses_when_remnant_wrong_shape(self):
        self._sign()
        self.gs.define_slot("SR", "gray", True)
        self.gs.submit_quantity(
            "q2", "T2", 1, ["SR"],
            [dict(m, slot="SR") for m in base_measurements()],
        )
        self.gs.sign("S2", "SR", "p")
        self.gs.register_stock(
            "B1",
            [{"id": "S", "width": 2, "height": 2, "grain": "vertical",
              "color": "gray"}],
            1,
        )
        self.gs.plan("P1", "B1", ["S1", "S2"])
        # Hand over a horizontally halved sheet: cannot make the tall pane.
        self.gs.register_stock(
            "B1R",
            [{"id": "H1", "width": 2, "height": 1, "grain": "vertical",
              "color": "gray", "y": 0},
             {"id": "H2", "width": 2, "height": 1, "grain": "vertical",
              "color": "gray", "y": 1}],
            1,
        )
        from glass.cutting import PlanError
        with self.assertRaises(PlanError):
            self.gs.resume("P1", "B1R", delivered=["S1"])


if __name__ == "__main__":
    unittest.main()

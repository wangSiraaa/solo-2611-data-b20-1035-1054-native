import unittest
from pathlib import Path

from cs import dataset, exhibit

DATA = Path(__file__).resolve().parent.parent / "materials" / "rights" / "dataset.json"


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = dataset.load(DATA)

    def batch(self, sid):
        return dataset.batch(self.ds, self.ds.scenario(sid))

    def test_worked_example_ships_cell_3(self):
        res = self.batch("A-worked")
        self.assertEqual(res.status, "delivered")
        self.assertEqual(res.solved[0].composition.id, "LA-c2")
        self.assertEqual(sorted(res.solved[0].check.printed), [(0, 3)])

    def test_emblem_blocks_both(self):
        res = self.batch("C-emblem")
        self.assertEqual(res.status, "blocked")

    def test_joint_completion_blocked(self):
        res = self.batch("D-joint")
        self.assertEqual(res.status, "blocked")
        self.assertEqual(res.diagnostics[0]["blocking_label"], "D2")

    def test_preference_is_lexicographic(self):
        res = self.batch("I-preference")
        self.assertEqual(res.status, "delivered")
        self.assertEqual(
            [s.composition.id for s in res.solved], ["I1-c2", "I2-c1"]
        )

    def test_intersection_limits_region(self):
        c = self.ds.license_for("T-Z")
        self.assertEqual(sorted(c.region), [(0, 0), (0, 1), (0, 2)])
        self.assertEqual(c.attribution, ["甲", "乙"])

    def test_union_of_parallel_nodes(self):
        c = self.ds.license_for("T-U")
        self.assertEqual(c.holders[0].kept_nodes, ["A", "B"])
        self.assertIn((1, 2), c.region)

    def test_hold(self):
        c = self.ds.license_for("T-HOLD")
        self.assertEqual(c.status, "held")
        self.assertIn("H9", c.held_holders)

    def test_edge_deletion_shrinks_region(self):
        edges2 = [e for e in self.ds.edges if e != ("X", "B")]
        c = self.ds.license_for("T-X", edges=edges2)
        self.assertNotIn((0, 3), c.region)

    def test_caps_respected(self):
        caps = self.ds.raw["caps"]
        branches = {l["branch"] for s in self.ds.raw["scenarios"] for l in s["labels"]}
        self.assertLessEqual(len(branches), caps["branches"])
        for scen in self.ds.raw["scenarios"]:
            self.assertLessEqual(len(scen["labels"]), caps["labels"])
            for label in scen["labels"]:
                self.assertLessEqual(len(label["compositions"]), caps["compositions"])

    def test_flip_maps_back_to_same_original(self):
        scen = self.ds.scenario("H-identity")
        labels = self.ds.label_objects(scen)
        pub = dataset.initial_public(self.ds, scen)
        h2 = labels[1]
        ch = dataset.check_composition(self.ds, h2, h2.compositions[0], pub)
        self.assertTrue(ch.feasible)
        # flipped placement still reports original cells (0,2) and (0,3)
        self.assertEqual(sorted(ch.printed), [(0, 2), (0, 3)])


if __name__ == "__main__":
    unittest.main()

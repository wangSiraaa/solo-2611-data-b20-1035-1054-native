import unittest

from museum.weights import WeightCSP, WeightError


class WeightCSPTests(unittest.TestCase):
    def build(self):
        csp = WeightCSP()
        csp.declare_period("A", [1, 3])
        csp.declare_period("B", [1, 3])
        return csp

    def test_simple_witness(self):
        csp = self.build()
        csp.add_constraint("P0", {"A": 1, "B": 1}, 4, 4)
        witness = csp.solve()
        self.assertEqual(witness["A"] + witness["B"], 4)
        self.assertTrue(csp.satisfiable())

    def test_empty_csp_returns_full_mass_table(self):
        csp = self.build()
        self.assertEqual(csp.solve(), {"A": 1, "B": 1})

    def test_hard_contradiction_conflict_core(self):
        csp = self.build()
        csp.add_constraint("pA4", {"A": 1}, 4, 4)  # 4 not in {1,3}
        core = csp.conflict_core()
        self.assertEqual([c.receipt_id for c in core], ["pA4"])

    def test_conflict_core_is_minimal(self):
        csp = self.build()
        csp.add_constraint("P0", {"A": 1, "B": 1}, 4, 4)
        csp.add_constraint("rwA", {"A": 1}, 1, 1)
        csp.add_constraint("rwB", {"B": 1}, 1, 1)
        self.assertFalse(csp.satisfiable())
        core = csp.conflict_core()
        ids = [c.receipt_id for c in core]
        self.assertEqual(set(ids), {"P0", "rwA", "rwB"})
        # Minimality: removing any one receipt restores satisfiability.
        for drop in ids:
            trial = self.build()
            for constraint in csp.constraints:
                if constraint.receipt_id != drop:
                    trial.add_constraint(
                        constraint.receipt_id, constraint.counts,
                        constraint.lo, constraint.hi,
                    )
            self.assertTrue(trial.satisfiable(), f"{drop} should be essential")

    def test_query_reports_conflict_group(self):
        csp = self.build()
        csp.add_constraint("P0", {"A": 1, "B": 1}, 4, 4)
        csp.add_constraint("rwA", {"A": 1}, 1, 1)
        csp.add_constraint("rwB", {"B": 1}, 1, 1)
        report = csp.query()
        self.assertFalse(report["satisfiable"])
        self.assertIn("P0", report["conflicting_receipts"])
        self.assertIsNone(report["masses"])

    def test_check_fixed(self):
        csp = self.build()
        csp.add_constraint("P0", {"A": 1, "B": 1}, 4, 4)
        self.assertEqual(csp.check_fixed({"A": 1, "B": 3}), [])
        broken = csp.check_fixed({"A": 3, "B": 3})
        self.assertEqual([c.receipt_id for c in broken], ["P0"])

    def test_illegal_declarations(self):
        csp = WeightCSP()
        with self.assertRaises(WeightError):
            csp.declare_period("X", [])
        with self.assertRaises(WeightError):
            csp.declare_period("X", [0, 1])
        csp.declare_period("X", [1, 2])
        with self.assertRaises(WeightError):
            csp.declare_period("X", [3, 4])

    def test_bad_constraint(self):
        csp = self.build()
        with self.assertRaises(WeightError):
            csp.add_constraint("bad", {"Z": 1}, 1, 2)
        with self.assertRaises(WeightError):
            csp.add_constraint("bad", {"A": 1}, 5, 2)

    def test_interval_width_gives_choices(self):
        csp = self.build()
        csp.add_constraint("loose", {"A": 1}, 1, 3)
        self.assertTrue(csp.satisfiable())
        report = csp.query(periods=["A"])
        self.assertEqual(list(report["masses"]), ["A"])


if __name__ == "__main__":
    unittest.main()

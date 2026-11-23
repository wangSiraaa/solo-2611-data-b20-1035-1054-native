import os
import tempfile
import unittest

from cs.store import Store
from museum.scale import PlateError, ScaleError, ScaleOffice

CATALOG = {
    "format": "classification-catalog/1",
    "revision": "G1",
    "nodes": [
        {"id": "ROOT", "name": "全部"},
        {"id": "A", "name": "铸期甲"},
        {"id": "B", "name": "铸期乙"},
        {"id": "A1", "name": "甲子类"},
        {"id": "X", "name": "跨期类"},
    ],
    "edges": [["A", "ROOT"], ["B", "ROOT"], ["A1", "A"], ["X", "A"], ["X", "B"]],
}

TICKETS = [
    {"ticket_id": "T-A", "revision": 1, "amount": 1, "classes": ["A1"]},
    {"ticket_id": "T-B", "revision": 1, "amount": 1, "classes": ["B"]},
    {"ticket_id": "T-X", "revision": 1, "amount": 1, "classes": ["X"]},
    {"ticket_id": "T-W", "revision": 1, "amount": 2, "classes": ["A1"]},
]


class ScaleOfficeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store.open(os.path.join(self.tmp, "cs.db"))
        self.store.init_catalog(CATALOG)
        self.store.apply_fact_revisions(TICKETS, request_id="seed")
        self.store.withdraw("T-W", 1, request_id="wd")
        self.office = ScaleOffice.open(os.path.join(self.tmp, "scale.db"), self.store)
        self.office.declare_domains({"A": [1, 3], "B": [1, 3]})

    def tearDown(self):
        self.store.close()
        self.office.close()

    def test_sign_basic_plate_and_pins_cut(self):
        result = self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        self.assertEqual(result.state, "signed")
        self.assertEqual(result.counts, {"A": 1, "B": 1})
        # The seed revision is F1 and the withdrawal opens F2; the plate is
        # pinned to whatever the current cut is at signing time.
        self.assertEqual(result.cut, self.store.current_cut().as_dict())

    def test_multi_period_row_rejected(self):
        result = self.office.sign_plate("PX", ["T-X"], (1, 3))
        self.assertEqual(result.state, "rejected")
        self.assertTrue(any("multiple distinct periods" in e for e in result.errors))
        self.assertEqual(self.office.list_receipts(), [])

    def test_withdrawn_row_not_loaded(self):
        result = self.office.sign_plate("PW", ["T-W"], (1, 6))
        self.assertEqual(result.state, "rejected")
        self.assertTrue(any("withdrawn row not placed" in w for w in result.warnings))

    def test_contradictory_plate_refused_and_not_stored(self):
        self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        bad = self.office.sign_plate("PA3", ["T-A"], (3, 3))
        self.assertEqual(bad.state, "rejected")
        ids = [r["receipt_id"] for r in self.office.list_receipts()]
        self.assertEqual(ids, ["P0"])

    def test_compatible_plate_accepted(self):
        self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        # Established witness is A=1, B=3; a single-A plate spanning that
        # weight is admitted, while one excluding it is refused separately.
        ok = self.office.sign_plate("Pspan", ["T-A"], (1, 1))
        self.assertEqual(ok.state, "signed")
        narrow = self.office.sign_plate("Pnarrow", ["T-A"], (3, 3))
        self.assertEqual(narrow.state, "rejected")

    def test_joint_appraisal_and_compatibility(self):
        self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        review = self.office.joint_appraisal(["P0"])
        self.assertEqual(review["verdict"], "compatible")
        report = self.office.compatibility()
        self.assertTrue(report["satisfiable"])
        self.assertEqual(report["masses"]["A"] + report["masses"]["B"], 4)

    def test_old_plate_survives_later_ticket_correction(self):
        first = self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        self.store.apply_fact_revision("T-A", 2, 9, ["A1"], request_id="fix")
        old = self.office.get_plate("P0")
        self.assertEqual(old["cut"], first.cut)
        self.assertEqual(old["counts"], {"A": 1, "B": 1})

    def test_duplicate_plate_idempotent(self):
        first = self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        second = self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        self.assertEqual(second.state, first.state)

    def test_duplicate_plate_conflicting_content_rejected(self):
        self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        with self.assertRaises(PlateError):
            self.office.sign_plate("P0", ["T-A"], (1, 3))

    def test_unknown_receipt_joint_review(self):
        with self.assertRaises(ScaleError):
            self.office.joint_appraisal(["NOPE"])

    def test_check_mass_table(self):
        self.office.sign_plate("P0", ["T-A", "T-B"], (4, 4))
        ok = self.office.check_mass_table({"A": 1, "B": 3})
        self.assertTrue(ok["supported"])
        bad = self.office.check_mass_table({"A": 3, "B": 3})
        self.assertFalse(bad["supported"])
        self.assertEqual(bad["violated_receipts"], ["P0"])


if __name__ == "__main__":
    unittest.main()

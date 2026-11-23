import unittest

from museum.lineage import LineageError, LineageLedger

DOMAINS = {"A": [1, 3], "B": [1, 3], "C": [2, 5]}
SCALE = [{"receipt_id": "P0", "counts": {"A": 1, "B": 1}, "lo": 4, "hi": 4}]


def happy_ledger() -> LineageLedger:
    ledger = LineageLedger()
    ledger.dispatch("B0", "S0", "YI", {"A": 1, "B": 1})
    ledger.forward("B1", "S0", "YI", "BING")
    ledger.split_merge(
        "B2",
        "BING",
        ["S0"],
        [
            {"seal_id": "a", "holder": "YI", "contents": {"A": 1}},
            {"seal_id": "b", "holder": "BING", "contents": {"B": 1}},
        ],
        authorized_open=True,
    )
    return ledger


class LineageRuleTests(unittest.TestCase):
    def test_dispatch_and_traversal(self):
        ledger = happy_ledger()
        self.assertEqual(ledger.seals["S0"].surrendered, True)
        self.assertEqual(ledger.children_of("S0"), ["a", "b"])
        self.assertTrue(all(not s.surrendered for s in ledger.live_seals()))

    def test_coin_ids_follow_physical_coins(self):
        ledger = happy_ledger()
        self.assertEqual(ledger.seals["a"].coin_ids, {"A": ["S0:A#1"]})
        self.assertEqual(ledger.seals["b"].coin_ids, {"B": ["S0:B#1"]})

    def test_forward_requires_holder(self):
        ledger = LineageLedger()
        ledger.dispatch("B0", "S0", "YI", {"A": 1})
        with self.assertRaises(LineageError):
            ledger.forward("BX", "S0", "BING", "YI")

    def test_split_merge_requires_open_authorization(self):
        ledger = LineageLedger()
        ledger.dispatch("B0", "S0", "YI", {"A": 1})
        with self.assertRaises(LineageError):
            ledger.split_merge(
                "BX", "YI", ["S0"],
                [{"seal_id": "x", "holder": "YI", "contents": {"A": 1}}],
                authorized_open=False,
            )

    def test_per_period_conservation(self):
        ledger = LineageLedger()
        ledger.dispatch("B0", "S0", "YI", {"A": 2})
        with self.assertRaises(LineageError):
            ledger.split_merge(
                "BX", "YI", ["S0"],
                [{"seal_id": "x", "holder": "YI", "contents": {"A": 1}}],
                authorized_open=True,
            )

    def test_old_seal_surrendered_only_once(self):
        ledger = happy_ledger()
        with self.assertRaises(LineageError):
            ledger.return_seal("S0", "BING")

    def test_reweigh_requires_holder(self):
        ledger = happy_ledger()
        with self.assertRaises(LineageError):
            ledger.reweigh("RW", "a", "BING", (1, 1))

    def test_duplicate_batch_id_rejected(self):
        ledger = LineageLedger()
        ledger.dispatch("B0", "S0", "YI", {"A": 1})
        with self.assertRaises(LineageError):
            ledger.dispatch("B0", "S1", "YI", {"A": 1})

    def test_merge_two_seals_conserves(self):
        ledger = LineageLedger()
        ledger.dispatch("B0", "S1", "YI", {"A": 1})
        ledger.dispatch("B1", "S2", "YI", {"B": 1})
        ledger.split_merge(
            "BM", "YI", ["S1", "S2"],
            [{"seal_id": "M", "holder": "YI", "contents": {"A": 1, "B": 1}}],
            authorized_open=True,
        )
        self.assertEqual(ledger.seals["M"].contents, {"A": 1, "B": 1})

    def test_snapshot_restore_roundtrip(self):
        ledger = happy_ledger()
        data = ledger.snapshot()
        clone = LineageLedger.restore(data)
        self.assertEqual(clone.snapshot(), data)


class ReconcileTests(unittest.TestCase):
    def test_happy_reconcile_with_shared_masses(self):
        ledger = happy_ledger()
        ledger.reweigh("RW-a", "a", "YI", (1, 1))
        ledger.reweigh("RW-b", "b", "BING", (3, 3))
        ledger.return_seal("a", "YI")
        ledger.return_seal("b", "BING")
        report = ledger.reconcile(DOMAINS, SCALE)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(
            report["weight_report"]["masses"], {"A": 1, "B": 3, "C": 2}
        )
        self.assertEqual(report["in_transit_seals"], [])
        self.assertIn("S0:A#1", report["coin_traces"])

    def test_in_transit_branch_flagged(self):
        ledger = happy_ledger()
        ledger.return_seal("a", "YI")  # b never comes back
        report = ledger.reconcile(DOMAINS, SCALE)
        self.assertFalse(report["ok"])
        self.assertEqual(report["in_transit_seals"], ["b"])
        self.assertTrue(any(p["code"] == "missing_in_transit" for p in report["problems"]))

    def test_global_weight_conflict(self):
        ledger = happy_ledger()
        ledger.reweigh("RW-a", "a", "YI", (1, 1))
        ledger.reweigh("RW-b", "b", "BING", (1, 1))
        ledger.return_seal("a", "YI")
        ledger.return_seal("b", "BING")
        report = ledger.reconcile(DOMAINS, SCALE)
        self.assertFalse(report["ok"])
        conflict = next(p for p in report["problems"] if p["code"] == "weight_conflict")
        self.assertIn("RW-a", conflict["conflicting_receipts"])
        self.assertIn("RW-b", conflict["conflicting_receipts"])

    def test_proposed_mass_table_support(self):
        ledger = happy_ledger()
        ledger.reweigh("RW-a", "a", "YI", (1, 1))
        ledger.return_seal("a", "YI")
        ledger.return_seal("b", "BING")
        ok = ledger.reconcile(DOMAINS, SCALE, proposed_masses={"A": 1, "B": 3, "C": 2})
        self.assertTrue(ok["ok"])
        bad = ledger.reconcile(DOMAINS, SCALE, proposed_masses={"A": 3, "B": 3, "C": 2})
        self.assertFalse(bad["ok"])
        self.assertTrue(any(p["code"] == "weight_conflict" for p in bad["problems"]))

    def test_double_spend_blocked(self):
        ledger = LineageLedger()
        ledger.dispatch("B0", "DS", "BING", {"B": 2})
        ledger.split_merge(
            "BS", "BING", ["DS"],
            [{"seal_id": "DS1", "holder": "BING", "contents": {"B": 2}}],
            authorized_open=True,
        )
        # The old seal is already dead; surrendering it again is rejected.
        with self.assertRaises(LineageError):
            ledger.return_seal("DS", "BING")
        report = ledger.reconcile(DOMAINS, [])
        self.assertFalse(report["ok"])
        self.assertEqual(report["in_transit_seals"], ["DS1"])


if __name__ == "__main__":
    unittest.main()

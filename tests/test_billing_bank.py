"""Tests for semester billing + bank reconciliation.

These encode the worked example from the task:

* two records ``r`` (2 pts) and ``s`` (2 pts) in registration order, paid by
  accounts 甲 (JIA) and 乙 (YI);
* first three points cost 2 each, every later point costs 1;
* original received: 甲 4, 乙 3; the bank folded 甲 4 + 乙 1 into flow ``P`` and
  took 乙 2 separately as ``Q``;
* after re-confirmation ``r`` becomes exempt, ``s`` stays charged, so the new
  receivable is 甲 0, 乙 4; 乙 authorizes a supplementary debit of exactly 2;
  the only feasible plan reverses the whole of ``P`` and debits 乙 2 (not
  "refund 甲 4 then simply collect 1 from 乙");
* if 乙's authorization is capped at 1, no plan is submittable and the desk
  performs **zero** bank actions.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit import (
    Store,
    bill_account_delta,
    parse_billing_policy,
    parse_caps,
    parse_flows,
    parse_records,
    plan_reconciliation,
    price_semester,
    sign_bill_document,
    verify_bill_signature,
)
from gradaudit.bank import LocalBankCounter, ResponseLost, SettlementDesk


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def ladder_policy():
    return parse_billing_policy({
        "format": "semester-billing/1",
        "semester": "2024F",
        "tiers": [{"up_to": 3, "unit_price": 2}, {"up_to": None, "unit_price": 1}],
        "exempt_courses": ["EXEMPT"],
        "record_accounts": {
            "r": {"account_id": "JIA", "sequence": 1},
            "s": {"account_id": "YI", "sequence": 2},
        },
    })


def rs_records():
    return parse_records({
        "student_id": "ST1", "revision": 1,
        "records": [
            {"record_id": "r", "external_course": "X", "points": 2},
            {"record_id": "s", "external_course": "X", "points": 2},
        ],
    })


def example_flows():
    return parse_flows({"format": "bank-flows/1", "flows": [
        {"reference": "P", "amounts": {"JIA": 4, "YI": 1}},
        {"reference": "Q", "amounts": {"YI": 2}},
    ]})


def caps(yi):
    return parse_caps({"format": "debit-authorizations/1", "caps": {"JIA": 0, "YI": yi}})


class TempBankMixin:
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".sqlite3")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #
class TestPricing(unittest.TestCase):
    def test_original_receipts_match_example(self):
        bill = price_semester(ladder_policy(), rs_records(), {"r": "X", "s": "X"})
        self.assertEqual(bill.accounts, {"JIA": 4, "YI": 3})
        by_rec = {line.record_id: line for line in bill.lines}
        # r occupies ladder positions 1-2 at price 2 => 4 to JIA
        self.assertEqual(by_rec["r"].amount, 4)
        self.assertEqual((by_rec["r"].start_position, by_rec["r"].end_position), (1, 2))
        # s straddles the band boundary: position 3 @2 + position 4 @1 => 3 to YI
        self.assertEqual(by_rec["s"].amount, 3)
        self.assertEqual([seg["points"] for seg in by_rec["s"].segments], [1, 1])
        self.assertEqual([seg["unit_price"] for seg in by_rec["s"].segments], [2, 1])

    def test_exempt_record_occupies_no_band(self):
        # r exempt, s unchanged points/account but shifts into the cheaper band.
        bill = price_semester(ladder_policy(), rs_records(), {"r": "EXEMPT", "s": "X"})
        self.assertEqual(bill.accounts, {"JIA": 0, "YI": 4})
        by_rec = {line.record_id: line for line in bill.lines}
        self.assertTrue(by_rec["r"].exempt)
        self.assertEqual(by_rec["r"].amount, 0)
        self.assertIsNone(by_rec["r"].start_position)
        # s now starts at position 1 and pays 2+2 = 4
        self.assertEqual((by_rec["s"].start_position, by_rec["s"].end_position), (1, 2))
        self.assertEqual(by_rec["s"].amount, 4)

    def test_unrelated_late_material_does_not_reorder(self):
        # Add a record registered later; existing charges are unchanged.
        records = parse_records({
            "student_id": "ST1", "revision": 1,
            "records": [
                {"record_id": "r", "external_course": "X", "points": 2},
                {"record_id": "s", "external_course": "X", "points": 2},
                {"record_id": "late", "external_course": "X", "points": 1},
            ],
        })
        policy = parse_billing_policy({
            "format": "semester-billing/1", "semester": "2024F",
            "tiers": [{"up_to": 3, "unit_price": 2}, {"up_to": None, "unit_price": 1}],
            "exempt_courses": [],
            "record_accounts": {
                "r": {"account_id": "JIA", "sequence": 1},
                "s": {"account_id": "YI", "sequence": 2},
                "late": {"account_id": "JIA", "sequence": 9},
            },
        })
        bill = price_semester(policy, records, {"r": "X", "s": "X", "late": "X"})
        by_rec = {line.record_id: line for line in bill.lines}
        self.assertEqual(by_rec["r"].start_position, 1)
        self.assertEqual(by_rec["s"].start_position, 3)
        self.assertEqual(by_rec["late"].start_position, 5)

    def test_exempt_not_counted_against_max_records_band(self):
        # Exempt records contribute no ladder points.
        bill = price_semester(ladder_policy(), rs_records(), {"r": "EXEMPT", "s": "EXEMPT"})
        self.assertEqual(bill.accounts, {"JIA": 0, "YI": 0})
        self.assertEqual(bill.total_charged_points, 0)

    def test_unknown_account_record_rejected(self):
        from gradaudit.errors import SchemaError
        policy = parse_billing_policy({
            "format": "semester-billing/1", "semester": "S",
            "tiers": [{"up_to": None, "unit_price": 1}],
            "exempt_courses": [],
            "record_accounts": {"r": {"account_id": "A", "sequence": 1}},
        })
        with self.assertRaises(SchemaError):
            price_semester(policy, rs_records(), {"r": "X", "s": "X"})


# --------------------------------------------------------------------------- #
# signed bill documents
# --------------------------------------------------------------------------- #
class TestSignedBill(unittest.TestCase):
    def test_signature_roundtrip(self):
        body = {"format": "semester-bill/1", "accounts": {"A": 4}, "lines": []}
        signed = sign_bill_document(body)
        self.assertTrue(verify_bill_signature(signed))
        tampered = dict(signed, accounts={"A": 5})
        self.assertFalse(verify_bill_signature(tampered))

    def test_account_delta_union(self):
        self.assertEqual(
            bill_account_delta({"A": 0, "B": 4}, {"A": 4, "B": 3}),
            {"A": -4, "B": 1},
        )
        self.assertEqual(bill_account_delta({"A": 4}, None), {"A": 4})


# --------------------------------------------------------------------------- #
# reconciliation (pure planning)
# --------------------------------------------------------------------------- #
class TestReconciliation(unittest.TestCase):
    def test_worked_example_reverses_p_and_debits_yi_2(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(2))
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.reversed_refs, ("P",))
        self.assertEqual(plan.rejected_refs, ("Q",))
        self.assertEqual(plan.debits, {"JIA": 0, "YI": 2})
        self.assertEqual(plan.final_balances, {"JIA": 0, "YI": 4})
        # Sanity: not "refund 甲 4 then simply collect 1 from 乙".
        self.assertNotIn(("Q",), [plan.reversed_refs])

    def test_cap_one_keeps_new_bill_and_has_no_plan(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(1))
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reversed_refs, ())
        self.assertEqual(plan.debits, {})

    def test_per_account_bounds_are_exposed(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(2))
        bounds = {b.account_id: b for b in plan.bounds}
        self.assertEqual(bounds["JIA"].min_reversal, 4)
        self.assertEqual(bounds["JIA"].max_reversal, 4)
        self.assertEqual(bounds["YI"].min_reversal, 0)
        self.assertEqual(bounds["YI"].max_reversal, 1)

    def test_avoid_trap_that_breaks_other_account_cap(self):
        flows = parse_flows({"format": "bank-flows/1", "flows": [
            {"reference": "A_Q", "amounts": {"X": 5, "Y": 4}},
            {"reference": "P", "amounts": {"X": 5}},
        ]})
        caps_doc = parse_caps({"format": "debit-authorizations/1",
                               "caps": {"X": 0, "Y": 0}})
        plan = plan_reconciliation({"X": 5, "Y": 4}, flows, caps_doc)
        self.assertTrue(plan.feasible)
        # A_Q would reverse Y=4 with cap 0; the only safe one-flow plan is P.
        self.assertEqual(plan.reversed_refs, ("P",))
        self.assertEqual(plan.debits, {"X": 0, "Y": 0})

    def test_prefers_fewest_flows(self):
        # X received 10, receivable 5 => must reverse exactly 5. Two ways:
        # a single 5-point flow, or two flows 2 + 3. Fewest = the single flow.
        flows = parse_flows({"format": "bank-flows/1", "flows": [
            {"reference": "one", "amounts": {"X": 5}},
            {"reference": "a", "amounts": {"X": 2}},
            {"reference": "b", "amounts": {"X": 3}},
        ]})
        caps_doc = parse_caps({"format": "debit-authorizations/1", "caps": {"X": 0}})
        plan = plan_reconciliation({"X": 5}, flows, caps_doc)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.reversed_refs, ("one",))

    def test_debits_never_exceed_caps_and_land_exact(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(2))
        self.assertTrue(all(plan.debits[a] <= c for a, c in caps(2).items()))
        self.assertEqual(plan.final_balances, {"JIA": 0, "YI": 4})


# --------------------------------------------------------------------------- #
# bank counter + settlement (execution)
# --------------------------------------------------------------------------- #
class TestBankCounter(TempBankMixin, unittest.TestCase):
    def test_infeasible_plan_performs_zero_bank_actions(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(1))
        with LocalBankCounter(self.db) as counter:
            desk = SettlementDesk(counter)
            result = desk.execute(plan, scope="s1")
            self.assertFalse(result.executed)
            self.assertEqual(result.actions, ())
            n = counter.conn.execute("SELECT COUNT(*) FROM bank_operations").fetchone()[0]
            self.assertEqual(n, 0)

    def test_feasible_plan_reverses_whole_flow_and_debits(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(2))
        with LocalBankCounter(self.db) as counter:
            desk = SettlementDesk(counter)
            result = desk.execute(plan, scope="s1")
            self.assertTrue(result.executed)
            kinds = [(a["kind"], a.get("reference"), a.get("account"), a.get("amount"))
                     for a in result.actions]
            self.assertIn(("reverse", "P", None, None), kinds)
            self.assertIn(("debit", "SUPP-s1-YI", "YI", 2), kinds)
            # Reversal is recorded at the *bank*, not as a local negative number.
            self.assertEqual(len(counter.query_reference("P")), 1)
            # Q stays untouched.
            self.assertEqual(counter.query_reference("Q"), [])

    def test_lost_response_reissue_completes_same_plan_once(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(2))
        # Lose every response: each op is applied, then ResponseLost; the desk
        # re-issues by the same operation id and finishes.
        with LocalBankCounter(self.db) as counter:
            desk = SettlementDesk(counter, lose_responses=True)
            result = desk.execute(plan, scope="s1")
            self.assertTrue(result.executed)
            statuses = [a["status"] for a in result.actions]
            self.assertTrue(all(s == "already_applied" for s in statuses))
            # Exactly one reversal of P, one debit of YI.
            self.assertEqual(len(counter.query_reference("P")), 1)
            self.assertEqual(
                counter.conn.execute(
                    "SELECT COUNT(*) FROM bank_operations WHERE kind='debit'"
                ).fetchone()[0],
                1,
            )

    def test_reopen_counter_and_query_by_reference(self):
        plan = plan_reconciliation({"JIA": 0, "YI": 4}, example_flows(), caps(2))
        with LocalBankCounter(self.db) as counter:
            SettlementDesk(counter).execute(plan, scope="s1")
        # A fresh instance over the same DB sees the durable bank record.
        with LocalBankCounter(self.db) as counter2:
            receipts = counter2.query_reference("P")
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].kind, "reverse")
            # Replaying the identical plan is idempotent (no double movement).
            replay = SettlementDesk(counter2).execute(plan, scope="s1")
            self.assertTrue(all(a["status"] == "already_applied" for a in replay.actions))
            self.assertEqual(len(counter2.query_reference("P")), 1)

    def test_local_negative_is_not_a_bank_refund(self):
        # Before executing anything, no bank record exists at all.
        with LocalBankCounter(self.db) as counter:
            self.assertEqual(counter.query_reference("P"), [])
            self.assertEqual(counter.query("REV-s1-P"), None)


# --------------------------------------------------------------------------- #
# end-to-end: store billing -> reconciliation
# --------------------------------------------------------------------------- #
class TestEndToEnd(unittest.TestCase):
    def _store_with_policy(self):
        store = Store(":memory:")
        store.import_catalog({
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "LC_X", "categories": ["CORE"]},
                {"course_id": "EXEMPT", "categories": ["CORE"]},
            ],
            "equivalences": {"X": ["LC_X", "EXEMPT"]},
        })
        store.import_records({
            "student_id": "ST1", "revision": 1,
            "records": [
                {"record_id": "r", "external_course": "X", "points": 2},
                {"record_id": "s", "external_course": "X", "points": 2},
            ],
        })
        store.import_requirements({
            "request_id": "REQ1", "revision": 1,
            "groups": [{"group_id": "G", "allowed_categories": ["CORE"], "minimum_points": 4}],
        })
        store.import_billing_policy({
            "format": "semester-billing/1", "semester": "2024F",
            "tiers": [{"up_to": 3, "unit_price": 2}, {"up_to": None, "unit_price": 1}],
            "exempt_courses": ["EXEMPT"],
            "record_accounts": {
                "r": {"account_id": "JIA", "sequence": 1},
                "s": {"account_id": "YI", "sequence": 2},
            },
        })
        return store

    def _confirm(self, store, r_course):
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            "assignments": [
                {"record_id": "r", "course_id": r_course, "group_id": "G"},
                {"record_id": "s", "course_id": "LC_X", "group_id": "G"},
            ],
        }
        prep = store.prepare_audit(plan, request_id="REQ1", catalog_revision=1,
                                   records_revision=1, requirements_revision=1)
        assert prep["accepted"], prep
        return store.confirm_audit(request_id="REQ1")

    def test_reconfirm_reprices_and_old_bill_readable(self):
        store = self._store_with_policy()
        c1 = self._confirm(store, "LC_X")
        b1 = store.issue_semester_bill(request_id="REQ1", semester="2024F")
        self.assertEqual(b1["accounts"], {"JIA": 4, "YI": 3})
        self.assertEqual(b1["account_delta"], {"JIA": 4, "YI": 3})
        self.assertTrue(store.verify_semester_bill(b1["bill_id"])["repriced_equal"])

        # Reopen, switch r to exempt; s unchanged.
        store.reopen_audit(request_id="REQ1", reason="r switched")
        self._confirm(store, "EXEMPT")
        b2 = store.issue_semester_bill(request_id="REQ1", semester="2024F")
        self.assertEqual(b2["accounts"], {"JIA": 0, "YI": 4})
        self.assertEqual(b2["account_delta"], {"JIA": -4, "YI": 1})
        self.assertEqual(b2["previous_bill_id"], b1["bill_id"])
        # s was re-billed into the cheap band even though its points/account
        # did not change.
        s_line = next(l for l in b2["lines"] if l["record_id"] == "s")
        self.assertEqual(s_line["amount"], 4)

        # Old bill stays readable verbatim and re-prices against its own
        # (original) confirmation.
        old = store.get_semester_bill(b1["bill_id"])
        self.assertEqual(old["accounts"], {"JIA": 4, "YI": 3})
        self.assertTrue(store.verify_semester_bill(b1["bill_id"])["repriced_equal"])
        # And bill against the original confirmation explicitly.
        b1_reissue = store.issue_semester_bill(
            request_id="REQ1", semester="2024F", confirmation_id=c1["confirmation_id"]
        )
        self.assertEqual(b1_reissue["accounts"], {"JIA": 4, "YI": 3})

    def test_bill_then_reconcile_to_exact_receivable(self):
        store = self._store_with_policy()
        self._confirm(store, "LC_X")
        b1 = store.issue_semester_bill(request_id="REQ1", semester="2024F")
        store.reopen_audit(request_id="REQ1", reason="r switched")
        self._confirm(store, "EXEMPT")
        b2 = store.issue_semester_bill(request_id="REQ1", semester="2024F")

        plan = plan_reconciliation(b2["accounts"], example_flows(), caps(2))
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.reversed_refs, ("P",))
        self.assertEqual(plan.final_balances, b2["accounts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

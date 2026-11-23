"""Tests for the transferable-credit ledger, exchange-window network, minimal
window suspension, and the offline appeal / hearing handover.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import os
import sys
import unittest
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit import (
    Store,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
    parse_positive_rational,
)
from gradaudit.errors import (
    LedgerError,
    ProtocolError,
    SchemaError,
    SuspensionError,
)
from gradaudit.handover import (
    build_packet,
    compare_ledger_and_roster,
    coverage_receipt,
    diff_judgements,
    ledger_judgements,
    multi_issue_thresholds,
    parse_roster,
    review_packet,
    roster_from_ledgers,
)
from gradaudit.ledger import build_ledger
from gradaudit.protocol import parse_protocol
from gradaudit.rationals import format_ratio, parse_positive_rational as R
from gradaudit.windows import (
    Network,
    Node,
    StartState,
    SuspensionRequest,
    Window,
    find_multi_issue,
    find_profits,
    parse_network,
    parse_suspension_request,
    reachable,
    solve_suspension,
    stamp,
)

# --------------------------------------------------------------------------- #
# shared fixtures: PU (浦校) -> HU (河校), A kind <-> B kind
# --------------------------------------------------------------------------- #
PU_A = Node("PU", "A")
HU_B = Node("HU", "B")


def two_window_network(revision: int = 1, ab: str = "2/3", ba: str = "2") -> Network:
    return Network(revision=revision, windows={
        "W_AB": Window("W_AB", PU_A, HU_B, R(ab)),
        "W_BA": Window("W_BA", HU_B, PU_A, R(ba)),
    })


CATALOG = {
    "format": "course-catalog/1", "revision": 1,
    "courses": [
        {"course_id": "PU_A1", "categories": ["A"]},
        {"course_id": "PU_A2", "categories": ["A"]},
        {"course_id": "PU_B1", "categories": ["B"]},
    ],
    "equivalences": {"PA": ["PU_A1", "PU_A2"], "PB": ["PU_B1"]},
}

PROTOCOL = {
    "format": "credit-protocol/1", "revision": 1,
    "entries": [
        {"course_id": "PU_A1", "issuing_school": "PU", "kind": "A", "ratio": "3/2"},
        {"course_id": "PU_A2", "issuing_school": "PU", "kind": "A", "ratio": "3/2"},
        {"course_id": "PU_B1", "issuing_school": "PU", "kind": "B", "ratio": "3/2"},
    ],
}


def records_doc(*entries):
    return {"student_id": "S1", "revision": 1, "records": list(entries)}


def reqs_doc(minimum: int = 2):
    return {"request_id": "REQ", "revision": 1, "groups": [
        {"group_id": "GA", "allowed_categories": ["A", "B"], "minimum_points": minimum},
    ]}


def plan_doc(assignments):
    return {
        "format": "course-allocation/1", "student_id": "S1",
        "catalog_revision": 1, "records_revision": 1,
        "request_id": "REQ", "requirements_revision": 1,
        "assignments": list(assignments),
    }


def confirmed_ledger(records, assignments, protocol=PROTOCOL, reqs=None):
    """Prepare + confirm a plan, then build the ledger from the confirmation."""
    store = Store(":memory:")
    store.import_catalog(CATALOG)
    store.import_records(records_doc(*records))
    store.import_requirements(reqs or reqs_doc())
    store.import_protocol(protocol)
    prep = store.prepare_audit(
        plan_doc(assignments), request_id="REQ",
        catalog_revision=1, records_revision=1, requirements_revision=1,
    )
    if not prep["accepted"]:
        raise AssertionError(f"fixture plan rejected: {prep['verification']}")
    store.confirm_audit(request_id="REQ")
    ledger = store.build_ledger("REQ", protocol_revision=1)
    store.close()
    return ledger


# The canonical "original" ledger: two used 1-point records both chosen A -> 3.
def original_ledger():
    return confirmed_ledger(
        [{"record_id": "R1", "external_course": "PA", "points": 1},
         {"record_id": "R2", "external_course": "PA", "points": 1}],
        [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
         {"record_id": "R2", "course_id": "PU_A2", "group_id": "GA"}],
    )


# =========================================================================== #
# exact rationals
# =========================================================================== #
class TestRationals(unittest.TestCase):
    def test_exact_forms(self):
        self.assertEqual(parse_positive_rational("3/2"), Fraction(3, 2))
        self.assertEqual(parse_positive_rational(2), Fraction(2))
        self.assertEqual(parse_positive_rational([3, 2]), Fraction(3, 2))
        self.assertEqual(parse_positive_rational({"numerator": 3, "denominator": 2}), Fraction(3, 2))
        self.assertEqual(parse_positive_rational(Fraction(1, 3)), Fraction(1, 3))

    def test_floats_rejected(self):
        with self.assertRaises(SchemaError):
            parse_positive_rational(1.5)
        with self.assertRaises(SchemaError):
            parse_positive_rational(0.1)

    def test_nonpositive_rejected(self):
        for bad in (0, -1, "0", "-3/2", [0, 1]):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                parse_positive_rational(bad)

    def test_format_ratio(self):
        self.assertEqual(format_ratio(Fraction(3, 2)), "3/2")
        self.assertEqual(format_ratio(Fraction(4)), "4")


# =========================================================================== #
# protocol + ledger
# =========================================================================== #
class TestLedger(unittest.TestCase):
    def test_two_records_pool_to_three_a_credits(self):
        ledger = original_ledger()
        pool = ledger.pool("PU", "A")
        self.assertEqual(pool.stampable, 3)
        self.assertEqual(pool.remainder, 0)
        self.assertEqual(pool.total_exact, Fraction(3))
        # every line timed as exact 3/2 then pooled, floored once
        self.assertEqual({it.exact_credits for it in pool.items}, {Fraction(3, 2)})
        self.assertEqual(ledger.total_stampable(), 3)

    def test_remainder_retained_not_moved_to_other_kind(self):
        # Each pool: 1 point x 3/2 = 1.5 -> stampable 1, remainder 1/2.
        # Remainders must stay on their own pool and never combine into credits.
        ledger = confirmed_ledger(
            [{"record_id": "R1", "external_course": "PA", "points": 1},
             {"record_id": "R2", "external_course": "PB", "points": 1}],
            [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
             {"record_id": "R2", "course_id": "PU_B1", "group_id": "GA"}],
        )
        a = ledger.pool("PU", "A")
        b = ledger.pool("PU", "B")
        self.assertEqual((a.stampable, b.stampable), (1, 1))
        self.assertEqual(a.remainder, Fraction(1, 2))
        self.assertEqual(b.remainder, Fraction(1, 2))
        # combined remainder is a full credit in total, but it is NOT stamped:
        self.assertEqual(a.stampable + b.stampable, 2)   # not 3
        self.assertEqual(a.remainder_voucher, "REMAINDER::PU::A")
        self.assertEqual(b.remainder_voucher, "REMAINDER::PU::B")
        # no cross-pool voucher
        self.assertNotEqual(a.remainder_voucher, b.remainder_voucher)

    def test_unused_record_opens_no_line(self):
        # R3 is present but never selected -> not on the ledger at all.
        ledger = confirmed_ledger(
            [{"record_id": "R1", "external_course": "PA", "points": 1},
             {"record_id": "R2", "external_course": "PA", "points": 1},
             {"record_id": "R3", "external_course": "PA", "points": 5}],
            [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
             {"record_id": "R2", "course_id": "PU_A2", "group_id": "GA"}],
        )
        self.assertIn("R3", ledger.unused_records)
        self.assertNotIn("R3", [it.record_id for p in ledger.pools.values() for it in p.items])
        # the unused 5-point record did not inflate the pool
        self.assertEqual(ledger.pool("PU", "A").stampable, 3)

    def test_missing_protocol_entry_errors(self):
        protocol = {"format": "credit-protocol/1", "revision": 1, "entries": [
            {"course_id": "PU_A1", "issuing_school": "PU", "kind": "A", "ratio": "3/2"},
        ]}
        with self.assertRaises(ProtocolError):
            confirmed_ledger(
                [{"record_id": "R1", "external_course": "PA", "points": 1},
                 {"record_id": "R2", "external_course": "PA", "points": 1}],
                [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
                 {"record_id": "R2", "course_id": "PU_A2", "group_id": "GA"}],
                protocol=protocol,
            )

    def test_ledger_requires_confirmation(self):
        store = Store(":memory:")
        store.import_catalog(CATALOG)
        store.import_records(records_doc(
            {"record_id": "R1", "external_course": "PA", "points": 1},
            {"record_id": "R2", "external_course": "PA", "points": 1}))
        store.import_requirements(reqs_doc())
        store.import_protocol(PROTOCOL)
        store.prepare_audit(
            plan_doc([{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
                      {"record_id": "R2", "course_id": "PU_A2", "group_id": "GA"}]),
            request_id="REQ", catalog_revision=1, records_revision=1, requirements_revision=1)
        # prepared but NOT confirmed
        with self.assertRaises(Exception):
            store.build_ledger("REQ", protocol_revision=1)
        store.close()

    def test_appeal_lines_expose_course_pool_remainder(self):
        ledger = confirmed_ledger(
            [{"record_id": "R1", "external_course": "PA", "points": 1},
             {"record_id": "R2", "external_course": "PB", "points": 1}],
            [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
             {"record_id": "R2", "course_id": "PU_B1", "group_id": "GA"}],
        )
        rows = {r["record_id"]: r for r in ledger.appeal_lines()}
        self.assertEqual(rows["R1"]["course_id"], "PU_A1")
        self.assertEqual(rows["R1"]["pool"], {"issuing_school": "PU", "kind": "A"})
        self.assertEqual(rows["R1"]["exact_credits"], "3/2")
        self.assertEqual(rows["R1"]["remainder_voucher"], "REMAINDER::PU::A")
        self.assertEqual(rows["R2"]["remainder_voucher"], "REMAINDER::PU::B")


# =========================================================================== #
# window network simulation
# =========================================================================== #
class TestWindows(unittest.TestCase):
    def test_stamp_floors_once(self):
        self.assertEqual(stamp(3, R("2/3")), 2)
        self.assertEqual(stamp(2, R("2")), 4)
        self.assertEqual(stamp(1, R("2/3")), 0)   # zero -> no voucher

    def test_multi_issue_three_to_four(self):
        net = two_window_network()
        walk = find_multi_issue(net, PU_A, 3, max_steps=2)
        self.assertIsNotNone(walk)
        self.assertEqual(walk.start_value, 3)
        self.assertEqual(walk.end_value, 4)
        self.assertEqual(walk.end_node, PU_A)
        self.assertEqual([s.value_in for s in walk.steps], [3, 2])
        self.assertEqual([s.value_out for s in walk.steps], [2, 4])

    def test_closing_return_window_stops_it_but_keeps_reachability(self):
        net = two_window_network()
        self.assertIsNone(find_multi_issue(net, PU_A, 3, max_steps=2, open_ids={"W_AB"}))
        self.assertTrue(reachable(net, PU_A, HU_B, open_ids={"W_AB"}))

    def test_product_above_one_but_floors_kill_small_value(self):
        # (3/2)^2 = 9/4 > 1, yet a 1-credit start dies: floor(1*3/2)=1 is not >1
        # even before the second floor.  Only >=2 can actually profit.
        net = Network(revision=1, windows={
            "WA": Window("WA", PU_A, HU_B, R("3/2")),
            "WB": Window("WB", HU_B, PU_A, R("3/2")),
        })
        self.assertGreater(R("3/2") * R("3/2"), 1)
        self.assertIsNone(find_multi_issue(net, PU_A, 1, max_steps=2))
        walk = find_multi_issue(net, PU_A, 2, max_steps=2)
        self.assertIsNotNone(walk)
        self.assertEqual([s.value_out for s in walk.steps], [3, 4])

    def test_zero_result_branch_dead(self):
        net = Network(revision=1, windows={
            "W0": Window("W0", PU_A, HU_B, R("1/2")),
            "Wback": Window("Wback", HU_B, PU_A, R("10")),
        })
        # 3 -> floor(1.5)=1 -> floor(10)=10 would profit, but a 1 start dies at
        # the first stop (floor(0.5)=0).  Zero issues no voucher.
        self.assertIsNone(find_multi_issue(net, PU_A, 1, max_steps=2))
        self.assertIsNotNone(find_multi_issue(net, PU_A, 3, max_steps=2))

    def test_shortest_witness_preferred(self):
        net = two_window_network()
        walk = find_multi_issue(net, PU_A, 3, max_steps=6)
        self.assertEqual(len(walk.steps), 2)

    def test_network_scale_limits(self):
        too_many = {"format": "exchange-windows/1", "revision": 1, "windows": [
            {"window_id": f"W{i}", "from": {"school": f"S{i}", "kind": "K"},
             "to": {"school": f"S{i}", "kind": "L"}, "ratio": 2}
            for i in range(11)
        ]}
        with self.assertRaises(SchemaError):
            parse_network(too_many)

    def test_joint_window_protection(self):
        net = Network(revision=1, windows={
            "W_AB": Window("W_AB", PU_A, HU_B, R("2/3")),
            "W_BA": Window("W_BA", HU_B, PU_A, R("2"), joint=True),
        })
        # Must keep A->B reachable and may not close the joint return window.
        # Closing W_AB would break reachability; W_BA is untouchable -> no
        # compliant closure.
        req = SuspensionRequest(
            max_steps=2,
            must_stay_reachable=((PU_A, HU_B),),
            protect_joint_windows=True,
        )
        res = solve_suspension(net, [StartState("poolA", PU_A, 3)], req)
        self.assertFalse(res.feasible)
        self.assertIn("W_BA", res.contradiction["protected_windows"])


# =========================================================================== #
# suspension solver
# =========================================================================== #
class TestSuspension(unittest.TestCase):
    def test_close_return_window_minimal(self):
        net = two_window_network()
        req = SuspensionRequest(max_steps=2, must_stay_reachable=((PU_A, HU_B),))
        res = solve_suspension(net, [StartState("A", PU_A, 3)], req)
        self.assertTrue(res.feasible)
        self.assertEqual(res.closed, ("W_BA",))
        self.assertEqual(res.after_profits, [])
        self.assertTrue(all(c["reachable"] for c in res.connectivity))
        # a concrete profit path is shown *before* suspension
        self.assertEqual(len(res.before_profits), 1)
        self.assertTrue(res.before_profits[0].walk.is_multi_issue)

    def test_restored_ledger_with_non_suspendable_return_is_contradiction(self):
        net = two_window_network()
        req = SuspensionRequest(
            max_steps=2,
            must_stay_reachable=((PU_A, HU_B),),
            non_suspendable=("W_BA",),
        )
        res = solve_suspension(net, [StartState("A", PU_A, 3)], req)
        self.assertFalse(res.feasible)
        self.assertIsNotNone(res.contradiction)
        # reachability is possible with all windows open; it is the joint
        # requirement (keep A->B AND stop multi-issue) that is incompatible.
        self.assertFalse(res.contradiction["reachability_impossible_even_fully_open"])
        self.assertFalse(res.contradiction["multi_issue_on_protected_windows"])
        self.assertIn("incompatible", res.contradiction["message"])

    def test_rerouted_second_record_needs_no_closure(self):
        # Only the second record is re-routed (via a fresh confirmed ledger on
        # the *same* network) to kind B.  Pools: A=1, B=1.  A=1 cannot move
        # (floor(2/3)=0), and kind B has no windows -> no closure needed.
        rerouted = confirmed_ledger(
            [{"record_id": "R1", "external_course": "PA", "points": 1},
             {"record_id": "R2", "external_course": "PB", "points": 1}],
            [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
             {"record_id": "R2", "course_id": "PU_B1", "group_id": "GA"}],
        )
        starts = [StartState("A", PU_A, rerouted.pool("PU", "A").stampable),
                  StartState("B", Node("PU", "B"), rerouted.pool("PU", "B").stampable)]
        self.assertEqual([s.value for s in starts], [1, 1])
        net = two_window_network()
        # A=1 -> floor(2/3)=0, so it "turns to zero" at Hu school; no window for B.
        res = solve_suspension(net, starts, SuspensionRequest(
            max_steps=2, must_stay_reachable=((PU_A, HU_B),)))
        self.assertTrue(res.feasible)
        self.assertEqual(res.closed, ())

    def test_minimal_closure_is_smallest_cardinality(self):
        # Two independent profitable loops; must close one window of each at
        # least -> minimum size 2.
        C = Node("C", "K"); D = Node("D", "K")
        net = Network(revision=1, windows={
            "AB": Window("AB", PU_A, HU_B, R("2/3")),
            "BA": Window("BA", HU_B, PU_A, R("2")),
            "CD": Window("CD", C, D, R("1/2")),
            "DC": Window("DC", D, C, R("3")),
        })
        # A: 3 -> 2 -> 4 profitable.  C: 2 -> 1 -> 3 profitable.
        starts = [StartState("A", PU_A, 3), StartState("C", C, 2)]
        res = solve_suspension(net, starts, SuspensionRequest(max_steps=2))
        self.assertTrue(res.feasible)
        self.assertEqual(len(res.closed), 2)
        # each loop broken (one window from each disjoint pair)
        self.assertTrue(set(res.closed) & {"BA", "AB"})
        self.assertTrue(set(res.closed) & {"CD", "DC"})

    def test_parse_suspension_request_roundtrip(self):
        doc = {
            "max_steps": 2,
            "must_stay_reachable": [{"from": {"school": "PU", "kind": "A"},
                                     "to": {"school": "HU", "kind": "B"}}],
            "non_suspendable": ["W_BA"],
            "protect_joint_windows": True,
        }
        req = parse_suspension_request(doc)
        self.assertEqual(req.max_steps, 2)
        self.assertEqual(req.non_suspendable, ("W_BA",))
        self.assertEqual(req.must_stay_reachable, ((PU_A, HU_B),))
        self.assertTrue(req.protect_joint_windows)


# =========================================================================== #
# offline handover: roster + coverage receipt, appeal/hearing consistency
# =========================================================================== #
class TestHandover(unittest.TestCase):
    def setUp(self):
        self.net = two_window_network()
        self.ledger = original_ledger()

    def test_roster_merges_equal_face_values(self):
        roster = roster_from_ledgers(
            [(self.ledger, "STU#1"), (original_ledger(), "STU#2")], revision=1)
        faces = roster.faces()
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0].face_value, 3)
        self.assertEqual(faces[0].holder_count, 2)

    def test_coverage_finds_real_multi_issue(self):
        roster = roster_from_ledgers([(self.ledger, "STU#1")])
        receipt = coverage_receipt(roster, self.net, max_steps=2)
        self.assertEqual(receipt.outcome, "multi_issue_found")
        self.assertFalse(receipt.all_covered)
        path = receipt.judgements[0].walk
        self.assertEqual(path.end_value, 4)
        # the path is concrete and re-checkable
        self.assertEqual([s.window_id for s in path.steps], ["W_AB", "W_BA"])

    def test_coverage_after_suspension(self):
        roster = roster_from_ledgers([(self.ledger, "STU#1")])
        receipt = coverage_receipt(roster, self.net, max_steps=2, closed_windows=["W_BA"])
        self.assertEqual(receipt.outcome, "coverage")
        self.assertTrue(receipt.all_covered)

    def test_appeal_and_hearing_agree(self):
        roster = roster_from_ledgers([(self.ledger, "STU#1")])
        report = compare_ledger_and_roster([(self.ledger, "STU#1")], roster, self.net, max_steps=2)
        self.assertTrue(report["consistent"], report)
        self.assertEqual(report["league_outcome"], "multi_issue_found")

    def test_two_students_same_amount_one_cancels(self):
        roster = roster_from_ledgers(
            [(self.ledger, "STU#1"), (original_ledger(), "STU#2")], revision=2)
        self.assertEqual(roster.faces()[0].holder_count, 2)
        self.assertTrue(roster.has_holders("PU", "A", 3))
        # cancel student 2 -> still issued (one holder remains)
        self.assertTrue(roster.remove_holder("PU", "A", 3, "STU#2"))
        self.assertEqual(roster.faces()[0].holder_count, 1)
        self.assertTrue(roster.has_holders("PU", "A", 3))
        # cancel last holder -> face value no longer issued
        self.assertTrue(roster.remove_holder("PU", "A", 3, "STU#1"))
        self.assertEqual(roster.entries, {})
        self.assertFalse(roster.has_holders("PU", "A", 3))
        # coverage over the now-empty roster trivially covers
        self.assertTrue(coverage_receipt(roster, self.net, max_steps=2).all_covered)

    def test_packet_offline_review_coverage(self):
        roster = roster_from_ledgers([(self.ledger, "STU#1")])
        packet = build_packet(roster, self.net, max_steps=2, closed_windows=["W_BA"])
        review = review_packet(packet.as_dict())
        self.assertEqual(review["outcome"], "coverage")
        self.assertTrue(review["all_covered"])
        self.assertTrue(review["shipped_receipt_matches"])

    def test_packet_offline_review_finds_multi_issue_without_suspension(self):
        roster = roster_from_ledgers([(self.ledger, "STU#1")])
        packet = build_packet(roster, self.net, max_steps=2)
        review = review_packet(packet.as_dict())
        self.assertEqual(review["outcome"], "multi_issue_found")
        self.assertEqual(len(review["violations"]), 1)
        self.assertEqual(review["violations"][0]["face_value"], 3)

    def test_ratio_revision_diff_which_rulings_still_apply(self):
        # Build a roster with face values 2 and 3 directly, then compare rulings
        # under two window revisions.
        #   old (2/3, 2):    2 -> 1 -> 2 (not >2) ; 3 -> 2 -> 4 (>3)
        #   new (3/2, 3/2):  2 -> 3 -> 4 (>2)     ; 3 -> 4 -> 6 (>3)
        # So face 2 is newly exploitable, face 3's ruling still applies.
        from gradaudit.handover import Roster
        roster = Roster(revision=1)
        roster.add_holder("PU", "A", 2, "STU#A")
        roster.add_holder("PU", "A", 3, "STU#B")
        old_net = two_window_network(revision=1, ab="2/3", ba="2")
        new_net = two_window_network(revision=2, ab="3/2", ba="3/2")
        old_rec = coverage_receipt(roster, old_net, max_steps=2)
        new_rec = coverage_receipt(roster, new_net, max_steps=2)
        diff = diff_judgements(old_rec, new_rec)
        self.assertEqual(diff["newly_multi_issue"], [{"school": "PU", "kind": "A", "face_value": 2}])
        self.assertEqual(diff["still_applies"], [{"school": "PU", "kind": "A", "face_value": 3}])
        self.assertEqual(diff["newly_covered"], [])
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["removed"], [])
        self.assertTrue(diff["changed"])

    def test_integer_threshold_table(self):
        net = two_window_network()
        # from PU/A: 2 -> 1 -> 2 (not >2); 3 -> 2 -> 4 (>3); 5 ->3->6 (>5)
        self.assertEqual(multi_issue_thresholds(net, PU_A, max_steps=2, up_to=5), [3, 5])

    def test_parse_roster_rejects_zero_holders(self):
        doc = {"format": "credit-roster/1", "revision": 1, "entries": [
            {"school": "PU", "kind": "A", "face_value": 3, "holders": []}]}
        with self.assertRaises(SchemaError):
            parse_roster(doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)

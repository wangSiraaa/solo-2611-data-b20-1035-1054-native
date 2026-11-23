#!/usr/bin/env python3
"""End-to-end walkthrough of the credit-ledger / exchange-window subsystem.

Reproduces, in order, the scenarios from the brief:

  1. Pu school, two used 1-credit records both chosen as kind A, ratio 3/2 each
     -> one A pool of 3 (exact timing, pooled, floored once).
  2. Windows A->B at 2/3 and B->A at 2: a 3-credit pool walks 3 -> 2 -> 4.
  3. Minimal suspension: keep A still reachable to B; only the return window
     may be closed -> close W_BA (1 window), profit gone, A->B still reachable.
  4. Re-route only the second record to kind B: pools A=1, B=1; A cannot move
     (floor(2/3)=0) and B has no windows -> no closure needed.
  5. Restore the original ledger, mark the return window non-suspendable while
     still requiring A->B => contradiction (no compliant closure).
  6. A ratio product above 1 (3/2 * 3/2 = 9/4) whose path still cannot profit a
     1-credit start because each stop floors (1 -> 1 is not an increase).

Then the offline handover: an anonymised roster + league coverage receipt that
an offline reviewer re-checks without the real ledgers, confirming either a real
multi-issue or post-suspension coverage; two students with equal amounts where
one cancels; and a ratio-revision diff showing which old rulings still apply.

Usage:  PYTHONPATH=. python3 examples/credit_demo.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gradaudit import Store
from gradaudit.handover import (
    build_packet,
    compare_ledger_and_roster,
    coverage_receipt,
    diff_judgements,
    review_packet,
    roster_from_ledgers,
)
from gradaudit.rationals import parse_positive_rational as R
from gradaudit.windows import (
    Network,
    Node,
    StartState,
    SuspensionRequest,
    Window,
    find_multi_issue,
    reachable,
    solve_suspension,
)

PU_A = Node("PU", "A")
HU_B = Node("HU", "B")

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


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def confirmed_ledger(records, assignments):
    store = Store(":memory:")
    store.import_catalog(CATALOG)
    store.import_records({"student_id": "S1", "revision": 1, "records": records})
    store.import_requirements({"request_id": "REQ", "revision": 1, "groups": [
        {"group_id": "GA", "allowed_categories": ["A", "B"], "minimum_points": 1},
    ]})
    store.import_protocol(PROTOCOL)
    plan = {
        "format": "course-allocation/1", "student_id": "S1",
        "catalog_revision": 1, "records_revision": 1,
        "request_id": "REQ", "requirements_revision": 1,
        "assignments": assignments,
    }
    prep = store.prepare_audit(plan, request_id="REQ", catalog_revision=1,
                               records_revision=1, requirements_revision=1)
    assert prep["accepted"], prep["verification"]
    store.confirm_audit(request_id="REQ")
    ledger = store.build_ledger("REQ", protocol_revision=1)
    store.close()
    return ledger


def two_window_network(revision=1, ab="2/3", ba="2", joint_ba=False):
    return Network(revision=revision, windows={
        "W_AB": Window("W_AB", PU_A, HU_B, R(ab)),
        "W_BA": Window("W_BA", HU_B, PU_A, R(ba), joint=joint_ba),
    })


def main():
    original = confirmed_ledger(
        [{"record_id": "R1", "external_course": "PA", "points": 1},
         {"record_id": "R2", "external_course": "PA", "points": 1}],
        [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
         {"record_id": "R2", "course_id": "PU_A2", "group_id": "GA"}],
    )

    banner("1. Pu school: two 1-credit records, both chosen kind A, ratio 3/2")
    pool = original.pool("PU", "A")
    for it in pool.items:
        print(f"  {it.record_id}: {it.points} pt x {it.ratio} = {it.exact_credits} exact")
    print(f"  pooled exact = {pool.total_exact}, floored once -> {pool.stampable} A credits")
    print(f"  remainder retained on A pool = {pool.remainder}")

    banner("2. Windows A->B 2/3, B->A 2: the 3-credit pool walks 3 -> 2 -> 4")
    net = two_window_network()
    walk = find_multi_issue(net, PU_A, pool.stampable, max_steps=2)
    for s in walk.steps:
        print(f"  {s.window_id}: {s.source.kind} {s.value_in} -> {s.target.kind} {s.value_out}")
    print(f"  returns to {walk.end_node.school}/{walk.end_node.kind} at {walk.end_value} "
          f"(strictly > {walk.start_value})")

    banner("3. Minimal suspension keeping A->B reachable")
    req = SuspensionRequest(max_steps=2, must_stay_reachable=((PU_A, HU_B),))
    res = solve_suspension(net, [StartState("A", PU_A, 3)], req)
    print(f"  feasible={res.feasible}, closed windows={list(res.closed)}")
    print(f"  profit after closing={len(res.after_profits)}, "
          f"A->B reachable={res.connectivity[0]['reachable']}")

    banner("4. Re-route only the second record to kind B (A=1, B=1)")
    rerouted = confirmed_ledger(
        [{"record_id": "R1", "external_course": "PA", "points": 1},
         {"record_id": "R2", "external_course": "PB", "points": 1}],
        [{"record_id": "R1", "course_id": "PU_A1", "group_id": "GA"},
         {"record_id": "R2", "course_id": "PU_B1", "group_id": "GA"}],
    )
    starts = [StartState("A", PU_A, rerouted.pool("PU", "A").stampable),
              StartState("B", Node("PU", "B"), rerouted.pool("PU", "B").stampable)]
    print(f"  pools: {[(s.label, s.value) for s in starts]}")
    print(f"  A=1 -> floor(1 * 2/3) = {1 * 2 // 3}: the A voucher turns to zero")
    res4 = solve_suspension(net, starts, req)
    print(f"  suspension feasible={res4.feasible}, closed={list(res4.closed)} (kind B has no windows)")

    banner("5. Restore original ledger; return window non-suspendable, still keep A->B")
    req5 = SuspensionRequest(max_steps=2, must_stay_reachable=((PU_A, HU_B),),
                             non_suspendable=("W_BA",))
    res5 = solve_suspension(net, [StartState("A", PU_A, 3)], req5)
    print(f"  feasible={res5.feasible}")
    print(f"  contradiction: {res5.contradiction['message']}")

    banner("6. Ratio product 9/4 > 1 yet a 1-credit start cannot profit")
    net6 = Network(revision=1, windows={
        "WA": Window("WA", PU_A, HU_B, R("3/2")),
        "WB": Window("WB", HU_B, PU_A, R("3/2")),
    })
    print(f"  (3/2)*(3/2) = {R('3/2') * R('3/2')} > 1")
    print(f"  start=1 multi-issue: {find_multi_issue(net6, PU_A, 1, max_steps=2)}")
    w6 = find_multi_issue(net6, PU_A, 2, max_steps=2)
    print(f"  start=2 walks {[s.value_out for s in w6.steps]} -> profitable only because 2 is large enough")

    banner("7. Offline handover: roster + league coverage receipt")
    roster = roster_from_ledgers([(original, "STU#1")], revision=1)
    receipt = coverage_receipt(roster, net, max_steps=2)
    print(f"  before suspension, roster face {roster.faces()[0].face_value}: {receipt.outcome}")
    packet = build_packet(roster, net, max_steps=2, closed_windows=["W_BA"])
    review = review_packet(packet.as_dict())
    print(f"  offline review after suspension: {review['outcome']}, "
          f"checks {review['checked_face_values']} face value(s), "
          f"shipped receipt matches={review['shipped_receipt_matches']}")
    print(f"  in-school appeal vs league hearing consistent: "
          f"{compare_ledger_and_roster([(original, 'STU#1')], roster, net, max_steps=2)['consistent']}")

    banner("8. Two students with equal amounts; one cancels")
    roster2 = roster_from_ledgers([(original, "STU#1"), (original, "STU#2")], revision=2)
    print(f"  holders of PU/A face 3: {roster2.faces()[0].holder_count}")
    roster2.remove_holder("PU", "A", 3, "STU#2")
    print(f"  after STU#2 cancels: {roster2.faces()[0].holder_count} holder, still issued="
          f"{roster2.has_holders('PU', 'A', 3)}")
    roster2.remove_holder("PU", "A", 3, "STU#1")
    print(f"  after STU#1 cancels: face 3 issued={roster2.has_holders('PU', 'A', 3)}, "
          f"coverage over empty roster={coverage_receipt(roster2, net, max_steps=2).all_covered}")

    banner("9. Window ratio revision: which old rulings still apply")
    roster3 = roster_from_ledgers([(original, "STU#B")])
    roster3.add_holder("PU", "A", 2, "STU#A")
    old_net = two_window_network(revision=1, ab="2/3", ba="2")
    new_net = two_window_network(revision=2, ab="3/2", ba="3/2")
    diff = diff_judgements(
        coverage_receipt(roster3, old_net, max_steps=2),
        coverage_receipt(roster3, new_net, max_steps=2),
    )
    print(f"  still applies: {diff['still_applies']}")
    print(f"  newly multi-issue after revision: {diff['newly_multi_issue']}")

    print("\nOK: all credit / window / handover scenarios completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

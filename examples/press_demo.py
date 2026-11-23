#!/usr/bin/env python3
"""End-to-end demonstration of the certificate-issuance subsystem.

Story, in order:

 1. the inter-school office reuses gradaudit's formal confirmation; the
    registry builds the master sheet (used records only, points from the
    pinned records revision);
 2. the terminology office serves the student: checks a proposed naming,
    offers legal alternatives, and explains "every row has a word, yet the
    book has none";
 3. the press planner answers the headline question: can the aid office get
    the public course without seeing the restricted one?  (Yes: red terms,
    ENROLL slip on top);
 4. the three failures: red approval withdrawn; the pass-all spare; the
    mixed-channel pair that would leak only the foreign name;
 5. one course on two confirmed records in two groups cannot take two words;
 6. the registry/counter handoff: a sealed package is pressed once; the top
    slip is torn away, the machine stops, a relief clerk resumes from the
    retained serials and action receipts; a damaged slip voids a package;
    the registry reviews which channels were blocked.

Usage:  PYTHONPATH=. python3 examples/press_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gradaudit import (
    Counter,
    FormSpec,
    PressFailure,
    PressPlan,
    RecipientSpec,
    RegistryOffice,
    Store,
    TermOffice,
    build_master_sheet,
    check_naming,
    explain_naming,
    plan_press,
    solve_namings,
)
from gradaudit.errors import CounterStateError, PackageError, PressError
from gradaudit.press import POINTS_CHANNEL

KEY = b"demo-shared-key"
ALL = frozenset({"WHITE", "RED", "BLUE", POINTS_CHANNEL})

XJ_01 = FormSpec("XJ-01", "ENROLL", ALL, frozenset({"RED"}))   # 学籍联: 截红
XJ_02 = FormSpec("XJ-02", "ENROLL", ALL, frozenset())          # 备用学籍联: 全透过
JZ_01 = FormSpec("JZ-01", "AID", ALL, frozenset())             # 奖助联
JZ_02 = FormSpec("JZ-02", "AID", ALL, frozenset())             # 备用奖助联
STOCK = [XJ_01, XJ_02, JZ_01, JZ_02]

RECIPIENTS = [
    RecipientSpec("ENROLL",
                  must_see=frozenset({("R1", "zh"), ("R1", "fl"),
                                      ("R2", "zh"), ("R2", "fl")}),
                  forbid=frozenset()),
    RecipientSpec("AID",
                  must_see=frozenset({("R1", "zh"), ("R1", "fl")}),
                  forbid=frozenset({("R2", "zh"), ("R2", "fl")})),
]


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def show_imprints(plan):
    for serial in plan.serials:
        form = next(f for f in plan.stack if f.serial == serial)
        print(f"  {serial} ({form.recipient}):")
        for e in plan.imprints_document(serial):
            print(f"    {e['record_id']} {e['field']:>3}: {e['value']}")


def make_registry():
    store = Store(":memory:")
    store.import_catalog({
        "format": "course-catalog/1", "revision": 1,
        "courses": [
            {"course_id": "LC_PUB", "categories": ["CORE"]},
            {"course_id": "LC_RES", "categories": ["CORE"]},
        ],
        "equivalences": {"PUBX": ["LC_PUB"], "RESX": ["LC_RES"]},
    })
    store.import_records({
        "student_id": "ST9", "revision": 1,
        "records": [
            {"record_id": "R1", "external_course": "PUBX", "points": 3},
            {"record_id": "R2", "external_course": "RESX", "points": 4},
            {"record_id": "R3", "external_course": "PUBX", "points": 2},
        ],
    })
    store.import_requirements({
        "request_id": "REQ9", "revision": 1,
        "groups": [{"group_id": "G_CORE", "allowed_categories": ["CORE"],
                    "minimum_points": 7}],
    })
    store.prepare_audit({
        "format": "course-allocation/1", "student_id": "ST9",
        "catalog_revision": 1, "records_revision": 1,
        "request_id": "REQ9", "requirements_revision": 1,
        "assignments": [
            {"record_id": "R1", "course_id": "LC_PUB", "group_id": "G_CORE"},
            {"record_id": "R2", "course_id": "LC_RES", "group_id": "G_CORE"},
        ],
    }, request_id="REQ9", catalog_revision=1, records_revision=1,
        requirements_revision=1)
    store.confirm_audit(request_id="REQ9")

    office = TermOffice(":memory:")
    office.register_pair(pair_id="PUB-W", course_id="LC_PUB",
                         zh="公共基础课", fl="Public Foundations",
                         zh_channel="WHITE", fl_channel="WHITE",
                         approved_groups=["G_CORE"])
    office.register_pair(pair_id="RES-R", course_id="LC_RES",
                         zh="涉限专业课", fl="Restricted Seminar",
                         zh_channel="RED", fl_channel="RED",
                         approved_groups=["G_CORE"])
    office.register_pair(pair_id="RES-B", course_id="LC_RES",
                         zh="涉限专业研讨", fl="Sensitive Topics",
                         zh_channel="BLUE", fl_channel="BLUE",
                         approved_groups=["G_CORE"])
    office.register_pair(pair_id="RES-MIX", course_id="LC_RES",
                         zh="涉限专业课（混）", fl="Restricted Seminar (mixed)",
                         zh_channel="RED", fl_channel="BLUE",
                         approved_groups=["G_CORE"])
    return RegistryOffice(store, office, key=KEY), office


def main() -> int:
    registry, office = make_registry()

    banner("1. Master sheet from the live confirmation (unused records stay out)")
    sheet = registry.build_sheet("REQ9")
    for r in sheet.rows:
        print(f"  {r.record_id}: {r.course_id} -> {r.group_id}, {r.points} pts")
    print("  (R3 was never used by the confirmed plan: not on the sheet)")

    banner("2. Terminology office serves the student individually")
    proposal = {"R1": "PUB-W", "R2": "RES-B"}
    print(f"  proposed naming {proposal}: "
          f"{'OK' if not check_naming(office, sheet, proposal) else 'REJECTED'}")
    print(f"  legal alternatives: {solve_namings(office, sheet)}")

    banner("2b. 'Every row has a word, yet the book has none'")
    dual_store = Store(":memory:")
    dual_store.import_catalog({
        "format": "course-catalog/1", "revision": 1,
        "courses": [{"course_id": "LC_DUAL", "categories": ["A", "B"]}],
        "equivalences": {"DUALX": ["LC_DUAL"]},
    })
    dual_store.import_records({
        "student_id": "ST10", "revision": 1,
        "records": [
            {"record_id": "R3", "external_course": "DUALX", "points": 2},
            {"record_id": "R4", "external_course": "DUALX", "points": 2},
        ],
    })
    dual_store.import_requirements({
        "request_id": "REQ10", "revision": 1,
        "groups": [
            {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 2},
            {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 2},
        ],
    })
    dual_store.prepare_audit({
        "format": "course-allocation/1", "student_id": "ST10",
        "catalog_revision": 1, "records_revision": 1,
        "request_id": "REQ10", "requirements_revision": 1,
        "assignments": [
            {"record_id": "R3", "course_id": "LC_DUAL", "group_id": "GA"},
            {"record_id": "R4", "course_id": "LC_DUAL", "group_id": "GB"},
        ],
    }, request_id="REQ10", catalog_revision=1, records_revision=1,
        requirements_revision=1)
    dual_store.confirm_audit(request_id="REQ10")
    dual_sheet = build_master_sheet(dual_store, "REQ10")
    dual_office = TermOffice(":memory:")
    dual_office.register_pair(pair_id="D-A", course_id="LC_DUAL",
                              zh="双组课甲", fl="Dual Course A",
                              zh_channel="WHITE", fl_channel="WHITE",
                              approved_groups=["GA"])
    dual_office.register_pair(pair_id="D-B", course_id="LC_DUAL",
                              zh="双组课乙", fl="Dual Course B",
                              zh_channel="WHITE", fl_channel="WHITE",
                              approved_groups=["GB"])
    reasons = check_naming(dual_office, dual_sheet, {"R3": "D-A", "R4": "D-B"})
    print(f"  one pair per row (D-A / D-B): rejected -> {reasons[0]['code']}")
    explanation = explain_naming(dual_office, dual_sheet)
    print(f"  row options: {explanation['row_options']}  (every row has a word)")
    for r in explanation["reasons"]:
        print(f"  [{r['code']}] {r['message']}")

    banner("3. Can AID get the public course yet never see the restricted one?")
    plan = plan_press(office, sheet, RECIPIENTS, STOCK)
    assert isinstance(plan, PressPlan)
    print(f"  YES -- naming LC_RES={plan.naming['LC_RES'].pair_id}, "
          f"stack top->bottom: {plan.serials}")
    show_imprints(plan)
    receipt = plan.receipt_for("AID")
    print(f"  AID receipt: {json.dumps(receipt['fields'], ensure_ascii=False)}")
    print("  -> the receipt carries only what AID's own slip shows; no leak.")

    banner("4a. Red approval withdrawn: blue prints alone, the pair cannot")
    office.withdraw_approval("RES-R", "G_CORE")
    solo = plan_press(office, sheet, RECIPIENTS[:1], STOCK)
    print(f"  ENROLL alone: feasible with LC_RES={solo.naming['LC_RES'].pair_id}")
    failure = plan_press(office, sheet, RECIPIENTS, STOCK)
    assert isinstance(failure, PressFailure)
    for r in failure.reasons:
        if r["code"] == "forbidden_imprinted":
            print(f"  blocked by: [{r['code']}] {r['message']}")
    office.restore_approval("RES-R", "G_CORE")

    banner("4b. ENROLL swapped to the pass-all spare: neither wording works")
    failure = plan_press(office, sheet, RECIPIENTS, [XJ_02, JZ_01])
    assert isinstance(failure, PressFailure)
    for r in failure.reasons:
        if r["code"] == "forbidden_imprinted":
            print(f"  [{r['code']}] {r['detail']}")

    banner("4c. Mixed-channel pair: leaking only the foreign name is stopped")
    office.withdraw_approval("RES-R", "G_CORE")
    office.withdraw_approval("RES-B", "G_CORE")
    failure = plan_press(office, sheet, RECIPIENTS, [XJ_01, JZ_01])
    assert isinstance(failure, PressFailure)
    for r in failure.reasons:
        if r["code"] == "forbidden_imprinted":
            print(f"  [{r['code']}] {r['message']}")
    office.restore_approval("RES-R", "G_CORE")
    office.restore_approval("RES-B", "G_CORE")

    banner("5. One course, two confirmed records, two groups: one word only")
    dual_office.register_pair(pair_id="D-ALL", course_id="LC_DUAL",
                              zh="双组课", fl="Dual Course",
                              zh_channel="WHITE", fl_channel="WHITE",
                              approved_groups=["GA", "GB"])
    print(f"  with D-ALL approved for both groups: "
          f"{solve_namings(dual_office, dual_sheet)}")

    banner("6. Sealed package: press once, stop midway, relief clerk resumes")
    tmp = tempfile.TemporaryDirectory()
    counter_db = os.path.join(tmp.name, "counter.sqlite3")
    package = registry.seal_package(sheet, RECIPIENTS, STOCK)
    print(f"  sealed {package['package_id']}: stack {package['stack']}, "
          f"naming {package['naming']}")
    counter = Counter(counter_db, key=KEY, stock=STOCK)
    session = counter.receive_package(package)
    print(f"  physicals check: {counter.verify_physicals(session)['ok']}")
    counter.press(session)
    top = counter.distribute(session)
    print(f"  torn away: {top['serial']} -> {top['recipient']}; machine stops")
    counter.stop(session)

    relief = Counter(counter_db, key=KEY, stock=STOCK)   # relief clerk, no grades
    resumed = relief.resume(session)
    print(f"  relief clerk resumes; pending slips: {resumed['pending_serials']}")
    relief.distribute(session)
    for serial in ("XJ-01", "JZ-01"):
        relief.register_receipt(session, serial)
    print(f"  session status: {relief.session_status(session)['status']}")

    review = registry.review_press(relief, session)
    print(f"  registry review -- blocked channels: {review['blocked_channels']}")

    try:
        relief.receive_package(registry.seal_package(sheet, RECIPIENTS, STOCK))
    except (CounterStateError, PackageError) as exc:
        print(f"  second press for the same sheet refused: {exc.code}")

    banner("6b. A damaged slip before pressing voids the package")
    other_store = Store(":memory:")
    # fresh student so the new sheet is independent of the pressed one
    other_store.import_catalog({
        "format": "course-catalog/1", "revision": 1,
        "courses": [
            {"course_id": "LC_PUB", "categories": ["CORE"]},
            {"course_id": "LC_RES", "categories": ["CORE"]},
        ],
        "equivalences": {"PUBX": ["LC_PUB"], "RESX": ["LC_RES"]},
    })
    other_store.import_records({
        "student_id": "ST11", "revision": 1,
        "records": [
            {"record_id": "R1", "external_course": "PUBX", "points": 3},
            {"record_id": "R2", "external_course": "RESX", "points": 4},
        ],
    })
    other_store.import_requirements({
        "request_id": "REQ11", "revision": 1,
        "groups": [{"group_id": "G_CORE", "allowed_categories": ["CORE"],
                    "minimum_points": 7}],
    })
    other_store.prepare_audit({
        "format": "course-allocation/1", "student_id": "ST11",
        "catalog_revision": 1, "records_revision": 1,
        "request_id": "REQ11", "requirements_revision": 1,
        "assignments": [
            {"record_id": "R1", "course_id": "LC_PUB", "group_id": "G_CORE"},
            {"record_id": "R2", "course_id": "LC_RES", "group_id": "G_CORE"},
        ],
    }, request_id="REQ11", catalog_revision=1, records_revision=1,
        requirements_revision=1)
    other_store.confirm_audit(request_id="REQ11")
    registry2 = RegistryOffice(other_store, office, key=KEY)
    sheet2 = registry2.build_sheet("REQ11")
    pkg = registry2.seal_package(sheet2, RECIPIENTS, STOCK)
    session2 = relief.receive_package(pkg)
    relief.report_damaged(session2, "JZ-01")
    print(f"  JZ-01 damaged -> package {pkg['package_id']} voided")
    try:
        relief.receive_package(pkg)
    except PackageError as exc:
        print(f"  reusing the voided package refused: {exc.code}")
    pkg2 = registry2.reseal_after_damage(pkg, sheet2, RECIPIENTS, STOCK,
                                         damaged_serial="JZ-01")
    print(f"  resealed {pkg2['package_id']}: stack {pkg2['stack']}")
    session3 = relief.receive_package(pkg2)
    relief.press(session3)
    print("  replacement package pressed OK")

    banner("7. Permit mode: the counter plans from the permit alone")
    permit = registry.issue_permit(sheet)
    from gradaudit import office_from_permit
    counter_office = office_from_permit(permit, key=KEY)
    plan2 = plan_press(counter_office, sheet, RECIPIENTS, STOCK)
    print(f"  permit constraints: {permit['constraints']}")
    naming2 = {cid: p.pair_id for cid, p in plan2.naming.items()}
    print(f"  counter-side plan: stack {plan2.serials}, naming {naming2}")
    print("  (a mere per-row allowed list would miss same-course and "
          "duplicate-name constraints)")

    print("\nOK: all issuance demonstrations completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Tests for the certificate-issuance subsystem (sheet / terms / press / counter).

The fixtures follow the acceptance scenario:

* one student with a live confirmation of two courses -- a public course
  (both languages printed on the WHITE channel) and a restricted course with
  two approved pairs, red-red and blue-blue;
* two recipient units: ENROLL (must see both courses' bilingual names) and
  AID (must see the public course's bilingual names, strictly forbidden the
  restricted course's two names);
* slips: XJ-01 (blocks RED), XJ-02 (passes everything), JZ-01 / JZ-02 (pass
  everything).  All slips show WHITE/RED/BLUE/PTS.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit import (
    Counter,
    CounterStateError,
    FormSpec,
    PackageError,
    PressError,
    PressFailure,
    PressPlan,
    RecipientSpec,
    RegistryOffice,
    SheetError,
    Store,
    TermOffice,
    build_master_sheet,
    check_distribution,
    check_naming,
    explain_naming,
    office_from_permit,
    plan_press,
    simulate_press,
    solve_namings,
)
from gradaudit.press import POINTS_CHANNEL, sheet_fields

KEY = b"registry-counter-shared-key"

ALL_CHANNELS = frozenset({"WHITE", "RED", "BLUE", POINTS_CHANNEL})

XJ_01 = FormSpec("XJ-01", "ENROLL", ALL_CHANNELS, frozenset({"RED"}))
XJ_02 = FormSpec("XJ-02", "ENROLL", ALL_CHANNELS, frozenset())       # spare: passes all
JZ_01 = FormSpec("JZ-01", "AID", ALL_CHANNELS, frozenset())
JZ_02 = FormSpec("JZ-02", "AID", ALL_CHANNELS, frozenset())          # spare
STOCK = [XJ_01, XJ_02, JZ_01, JZ_02]

RECIPIENTS = [
    RecipientSpec(
        recipient="ENROLL",
        must_see=frozenset({("R1", "zh"), ("R1", "fl"), ("R2", "zh"), ("R2", "fl")}),
        forbid=frozenset(),
    ),
    RecipientSpec(
        recipient="AID",
        must_see=frozenset({("R1", "zh"), ("R1", "fl")}),
        forbid=frozenset({("R2", "zh"), ("R2", "fl")}),
    ),
]

CATALOG = {
    "format": "course-catalog/1", "revision": 1,
    "courses": [
        {"course_id": "LC_PUB", "categories": ["CORE"]},
        {"course_id": "LC_RES", "categories": ["CORE"]},
    ],
    "equivalences": {"PUBX": ["LC_PUB"], "RESX": ["LC_RES"]},
}
RECORDS = {
    "student_id": "ST9", "revision": 1,
    "records": [
        {"record_id": "R1", "external_course": "PUBX", "points": 3},
        {"record_id": "R2", "external_course": "RESX", "points": 4},
        {"record_id": "R3", "external_course": "PUBX", "points": 2},  # unused
    ],
}
REQUIREMENTS = {
    "request_id": "REQ9", "revision": 1,
    "groups": [{"group_id": "G_CORE", "allowed_categories": ["CORE"], "minimum_points": 7}],
}
PLAN = {
    "format": "course-allocation/1", "student_id": "ST9",
    "catalog_revision": 1, "records_revision": 1,
    "request_id": "REQ9", "requirements_revision": 1,
    "assignments": [
        {"record_id": "R1", "course_id": "LC_PUB", "group_id": "G_CORE"},
        {"record_id": "R2", "course_id": "LC_RES", "group_id": "G_CORE"},
    ],
}


def make_store(plan_doc=PLAN, requirements=REQUIREMENTS):
    store = Store(":memory:")
    store.import_catalog(CATALOG)
    store.import_records(RECORDS)
    store.import_requirements(requirements)
    store.prepare_audit(plan_doc, request_id=requirements["request_id"],
                        catalog_revision=1, records_revision=1, requirements_revision=1)
    store.confirm_audit(request_id=requirements["request_id"])
    return store


def make_office():
    office = TermOffice(":memory:")
    office.register_pair(
        pair_id="PUB-W", course_id="LC_PUB",
        zh="公共基础课", fl="Public Foundations",
        zh_channel="WHITE", fl_channel="WHITE", approved_groups=["G_CORE"],
    )
    office.register_pair(
        pair_id="RES-R", course_id="LC_RES",
        zh="涉限专业课", fl="Restricted Seminar",
        zh_channel="RED", fl_channel="RED", approved_groups=["G_CORE"],
    )
    office.register_pair(
        pair_id="RES-B", course_id="LC_RES",
        zh="涉限专业研讨", fl="Sensitive Topics",
        zh_channel="BLUE", fl_channel="BLUE", approved_groups=["G_CORE"],
    )
    office.register_pair(
        pair_id="RES-MIX", course_id="LC_RES",
        zh="涉限专业课（混）", fl="Restricted Seminar (mixed)",
        zh_channel="RED", fl_channel="BLUE", approved_groups=["G_CORE"],
    )
    return office


def make_sheet():
    store = make_store()
    sheet = build_master_sheet(store, "REQ9")
    return store, sheet


def imprints_of(plan, serial):
    return {(e["record_id"], e["field"]): e["value"] for e in plan.imprints_document(serial)}


# =========================================================================== #
# master sheet from the live confirmation
# =========================================================================== #
class TestMasterSheet(unittest.TestCase):
    def test_rows_are_exactly_the_used_records(self):
        _, sheet = make_sheet()
        self.assertEqual([r.record_id for r in sheet.rows], ["R1", "R2"])  # R3 unused
        self.assertEqual([(r.course_id, r.group_id, r.points) for r in sheet.rows],
                         [("LC_PUB", "G_CORE", 3), ("LC_RES", "G_CORE", 4)])

    def test_points_come_from_the_pinned_records_revision(self):
        store, sheet = make_sheet()
        self.assertEqual({r.record_id: r.points for r in sheet.rows}, {"R1": 3, "R2": 4})
        # a later records revision makes the confirmation stale: no printing
        store.import_records({
            "student_id": "ST9", "revision": 2,
            "records": [
                {"record_id": "R1", "external_course": "PUBX", "points": 5},
                {"record_id": "R2", "external_course": "RESX", "points": 4},
                {"record_id": "R3", "external_course": "PUBX", "points": 2},
            ],
        })
        with self.assertRaises(SheetError):
            build_master_sheet(store, "REQ9")

    def test_no_confirmation_no_sheet(self):
        store = Store(":memory:")
        store.import_catalog(CATALOG)
        store.import_records(RECORDS)
        store.import_requirements(REQUIREMENTS)
        with self.assertRaises(SheetError):
            build_master_sheet(store, "REQ9")

    def test_external_systems_confirmed_allocation_same_path(self):
        """An external system's confirmed plan is printed per its actual choices."""
        req = {
            "request_id": "REQX", "revision": 1,
            "groups": [{"group_id": "G_CORE", "allowed_categories": ["CORE"],
                        "minimum_points": 3}],
        }
        external_plan = {
            "format": "course-allocation/1", "student_id": "ST9",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQX", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R2", "course_id": "LC_RES", "group_id": "G_CORE"},
            ],
        }
        store = make_store(plan_doc=external_plan, requirements=req)
        sheet = build_master_sheet(store, "REQX")
        self.assertEqual([r.record_id for r in sheet.rows], ["R2"])
        self.assertEqual(sheet.rows[0].points, 4)

    def test_at_most_eight_rows(self):
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [{"course_id": "LC_PUB", "categories": ["CORE"]}],
            "equivalences": {"PUBX": ["LC_PUB"]},
        }
        records = {
            "student_id": "ST8", "revision": 1,
            "records": [
                {"record_id": f"R{i}", "external_course": "PUBX", "points": 1}
                for i in range(1, 10)
            ],
        }
        req = {
            "request_id": "REQ8", "revision": 1,
            "groups": [{"group_id": "G_CORE", "allowed_categories": ["CORE"],
                        "minimum_points": 9}],
        }
        plan = {
            "format": "course-allocation/1", "student_id": "ST8",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ8", "requirements_revision": 1,
            "assignments": [
                {"record_id": f"R{i}", "course_id": "LC_PUB", "group_id": "G_CORE"}
                for i in range(1, 10)
            ],
        }
        store = Store(":memory:")
        store.import_catalog(catalog)
        store.import_records(records)
        store.import_requirements(req)
        store.prepare_audit(plan, request_id="REQ8",
                            catalog_revision=1, records_revision=1, requirements_revision=1)
        store.confirm_audit(request_id="REQ8")
        with self.assertRaises(SheetError):
            build_master_sheet(store, "REQ8")


# =========================================================================== #
# terminology office
# =========================================================================== #
class TestTerminology(unittest.TestCase):
    def test_legal_alternatives_and_row_level_check(self):
        _, sheet = make_sheet()
        office = make_office()
        # a proposed naming can be checked directly (individual service)
        self.assertEqual(check_naming(office, sheet, {"R1": "PUB-W", "R2": "RES-B"}), [])
        # legal alternatives are enumerable
        namings = solve_namings(office, sheet)
        self.assertIn({"LC_PUB": "PUB-W", "LC_RES": "RES-R"}, namings)
        self.assertIn({"LC_PUB": "PUB-W", "LC_RES": "RES-B"}, namings)

    def test_pair_must_cover_every_actual_group(self):
        _, sheet = make_sheet()
        office = make_office()
        office.withdraw_approval("RES-R", "G_CORE")
        reasons = check_naming(office, sheet, {"R1": "PUB-W", "R2": "RES-R"})
        self.assertTrue(any(r["code"] == "pair_not_approved_for_group" for r in reasons))

    def test_same_course_two_rows_cannot_take_two_pairs(self):
        """Two confirmed records of one course in different groups: one pair only."""
        sheet, office = self._dual_sheet(pairs=[
            ("D-A", ["GA"], "双组课甲", "Dual Course A"),
            ("D-B", ["GB"], "双组课乙", "Dual Course B"),
        ])
        reasons = check_naming(office, sheet, {"R3": "D-A", "R4": "D-B"})
        inconsistent = [r for r in reasons if r["code"] == "course_term_inconsistent"]
        self.assertTrue(inconsistent, reasons)
        self.assertEqual(inconsistent[0]["location"], "LC_DUAL")

    def test_every_row_has_a_word_yet_the_book_has_none(self):
        """Each row individually has an approved pair; no pair covers both groups."""
        sheet, office = self._dual_sheet(pairs=[
            ("D-A", ["GA"], "双组课甲", "Dual Course A"),
            ("D-B", ["GB"], "双组课乙", "Dual Course B"),
        ])
        self.assertEqual(solve_namings(office, sheet), [])
        explanation = explain_naming(office, sheet)
        self.assertFalse(explanation["solvable"])
        # every row does have a word
        self.assertEqual(explanation["row_options"], {"R3": ["D-A"], "R4": ["D-B"]})
        conflicts = [r for r in explanation["reasons"]
                     if r["code"] == "cross_group_pair_conflict"]
        self.assertTrue(conflicts, explanation)
        self.assertEqual(conflicts[0]["location"], "LC_DUAL")
        self.assertEqual(
            conflicts[0]["detail"]["approved_pairs_per_group"],
            {"GA": ["D-A"], "GB": ["D-B"]},
        )

    def test_one_pair_covering_both_groups_solves_the_book(self):
        sheet, office = self._dual_sheet(pairs=[
            ("D-A", ["GA"], "双组课甲", "Dual Course A"),
            ("D-B", ["GB"], "双组课乙", "Dual Course B"),
            ("D-ALL", ["GA", "GB"], "双组课", "Dual Course"),
        ])
        self.assertEqual(solve_namings(office, sheet), [{"LC_DUAL": "D-ALL"}])
        self.assertEqual(
            check_naming(office, sheet, {"R3": "D-ALL", "R4": "D-ALL"}), [],
        )

    def test_two_courses_may_not_print_as_one_pair(self):
        _, sheet = make_sheet()
        office = make_office()
        office.register_pair(
            pair_id="RES-COPY", course_id="LC_RES",
            zh="公共基础课", fl="Public Foundations",   # identical complete text
            zh_channel="RED", fl_channel="RED", approved_groups=["G_CORE"],
        )
        reasons = check_naming(office, sheet, {"R1": "PUB-W", "R2": "RES-COPY"})
        collisions = [r for r in reasons if r["code"] == "name_collision"]
        self.assertTrue(collisions, reasons)
        self.assertEqual(sorted(collisions[0]["detail"]["courses"]), ["LC_PUB", "LC_RES"])
        # the solver never proposes such a combination
        for naming in solve_namings(office, sheet):
            texts = set()
            for pid in naming.values():
                pair = office.get_pair(pid)
                self.assertNotIn((pair.zh, pair.fl), texts)
                texts.add((pair.zh, pair.fl))

    def _dual_sheet(self, pairs):
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [{"course_id": "LC_DUAL", "categories": ["A", "B"]}],
            "equivalences": {"DUALX": ["LC_DUAL"]},
        }
        records = {
            "student_id": "ST10", "revision": 1,
            "records": [
                {"record_id": "R3", "external_course": "DUALX", "points": 2},
                {"record_id": "R4", "external_course": "DUALX", "points": 2},
            ],
        }
        req = {
            "request_id": "REQ10", "revision": 1,
            "groups": [
                {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 2},
                {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 2},
            ],
        }
        plan = {
            "format": "course-allocation/1", "student_id": "ST10",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ10", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R3", "course_id": "LC_DUAL", "group_id": "GA"},
                {"record_id": "R4", "course_id": "LC_DUAL", "group_id": "GB"},
            ],
        }
        store = Store(":memory:")
        store.import_catalog(catalog)
        store.import_records(records)
        store.import_requirements(req)
        store.prepare_audit(plan, request_id="REQ10",
                            catalog_revision=1, records_revision=1, requirements_revision=1)
        store.confirm_audit(request_id="REQ10")
        sheet = build_master_sheet(store, "REQ10")
        office = TermOffice(":memory:")
        for pair_id, groups, zh, fl in pairs:
            office.register_pair(
                pair_id=pair_id, course_id="LC_DUAL", zh=zh, fl=fl,
                zh_channel="WHITE", fl_channel="WHITE", approved_groups=groups,
            )
        return sheet, office


# =========================================================================== #
# press planning: the acceptance scenario
# =========================================================================== #
class TestPressPlanning(unittest.TestCase):
    def test_red_terms_with_enroll_on_top_works(self):
        _, sheet = make_sheet()
        office = make_office()
        plan = plan_press(office, sheet, RECIPIENTS, STOCK)
        self.assertIsInstance(plan, PressPlan)
        self.assertEqual(plan.serials, ["XJ-01", "JZ-01"])
        self.assertEqual(plan.naming["LC_RES"].pair_id, "RES-R")
        self.assertEqual(plan.naming["LC_PUB"].pair_id, "PUB-W")

        xj = imprints_of(plan, "XJ-01")
        jz = imprints_of(plan, "JZ-01")
        # ENROLL sees both courses' bilingual names
        for rid in ("R1", "R2"):
            self.assertIn((rid, "zh"), xj)
            self.assertIn((rid, "fl"), xj)
        # AID sees the public course's names and (allowed) points ...
        self.assertEqual(jz[("R1", "zh")], "公共基础课")
        self.assertEqual(jz[("R1", "fl")], "Public Foundations")
        # ... but the restricted course's two names never reach JZ-01
        self.assertNotIn(("R2", "zh"), jz)
        self.assertNotIn(("R2", "fl"), jz)
        self.assertIn(("R2", "pts"), jz)  # points are allowed to be carried out
        # XJ-01 stopped the red channel for everything below it
        self.assertEqual(sorted(set(plan.outcome.blocked_channels["XJ-01"])), ["RED"])
        self.assertEqual(plan.outcome.blocked_channels["JZ-01"], [])

    def test_receipt_cannot_leak_the_confidential_names(self):
        _, sheet = make_sheet()
        office = make_office()
        plan = plan_press(office, sheet, RECIPIENTS, STOCK)
        receipt = plan.receipt_for("AID")
        blob = json.dumps(receipt, ensure_ascii=False)
        self.assertIn("公共基础课", blob)
        self.assertNotIn("涉限", blob)
        self.assertNotIn("Restricted", blob)
        self.assertNotIn("Sensitive", blob)

    def test_red_approval_withdrawn_pair_cannot_go_together(self):
        _, sheet = make_sheet()
        office = make_office()
        office.withdraw_approval("RES-R", "G_CORE")

        # blue still prints an ordinary certificate for ENROLL alone
        solo = plan_press(office, sheet, RECIPIENTS[:1], STOCK)
        self.assertIsInstance(solo, PressPlan)
        self.assertEqual(solo.naming["LC_RES"].pair_id, "RES-B")

        # but the two slips cannot be issued together: nobody blocks BLUE
        failure = plan_press(office, sheet, RECIPIENTS, STOCK)
        self.assertIsInstance(failure, PressFailure)
        forbidden = [r for r in failure.reasons if r["code"] == "forbidden_imprinted"]
        self.assertTrue(forbidden, failure.reasons)
        self.assertTrue(all(r["location"] == "AID" for r in forbidden))
        self.assertEqual({r["detail"]["record_id"] for r in forbidden}, {"R2"})

    def test_pass_all_spare_makes_both_wordings_unusable(self):
        _, sheet = make_sheet()
        office = make_office()
        stock = [XJ_02, JZ_01]  # the ENROLL spare blocks nothing
        failure = plan_press(office, sheet, RECIPIENTS, stock)
        self.assertIsInstance(failure, PressFailure)
        forbidden = [r for r in failure.reasons if r["code"] == "forbidden_imprinted"]
        self.assertEqual(
            {(r["detail"]["record_id"], r["detail"]["field"]) for r in forbidden},
            {("R2", "zh"), ("R2", "fl")},
        )

    def test_mixed_channel_pair_leaking_only_the_foreign_name_is_stopped(self):
        _, sheet = make_sheet()
        office = make_office()
        office.withdraw_approval("RES-R", "G_CORE")
        office.withdraw_approval("RES-B", "G_CORE")   # only RES-MIX remains
        failure = plan_press(office, sheet, RECIPIENTS, [XJ_01, JZ_01])
        self.assertIsInstance(failure, PressFailure)
        forbidden = [r for r in failure.reasons if r["code"] == "forbidden_imprinted"]
        # XJ-01 stops RED (the Chinese half) but BLUE carries the foreign
        # name straight through: exactly one leaked field remains
        self.assertEqual(
            [(r["location"], r["detail"]["record_id"], r["detail"]["field"])
             for r in forbidden],
            [("AID", "R2", "fl")],
        )

    def test_any_forbidden_imprint_is_a_mis_issuance(self):
        _, sheet = make_sheet()
        office = make_office()
        naming = {"LC_PUB": office.get_pair("PUB-W"), "LC_RES": office.get_pair("RES-R")}
        fields = sheet_fields(sheet, naming)
        # AID's slip on top: it shows RED, so both forbidden names imprint
        outcome = simulate_press(fields, [JZ_01, XJ_01])
        violations = check_distribution(outcome, [JZ_01, XJ_01], RECIPIENTS)
        self.assertEqual(
            {(v["detail"]["record_id"], v["detail"]["field"]) for v in violations
             if v["code"] == "forbidden_imprinted"},
            {("R2", "zh"), ("R2", "fl")},
        )

    def test_scale_limits(self):
        _, sheet = make_sheet()
        office = make_office()
        # seven recipients are one too many
        many = [RecipientSpec(f"U{i}", frozenset(), frozenset()) for i in range(7)]
        stock = [FormSpec(f"F{i}", f"U{i}", ALL_CHANNELS, frozenset()) for i in range(7)]
        with self.assertRaises(PressError):
            plan_press(office, sheet, many, stock)
        # three slips for one unit are one too many
        trio = STOCK + [FormSpec("JZ-03", "AID", ALL_CHANNELS, frozenset())]
        with self.assertRaises(PressError):
            plan_press(office, sheet, RECIPIENTS, trio)
        # contradictory requirements are rejected outright
        bad = [RecipientSpec("ENROLL", frozenset({("R1", "zh")}), frozenset({("R1", "zh")}))]
        with self.assertRaises(PressError):
            plan_press(office, sheet, bad, STOCK)


# =========================================================================== #
# sealed packages and the counter
# =========================================================================== #
class TestCounterHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.counter_db = os.path.join(self.tmp.name, "counter.sqlite3")
        self.store, self.sheet = make_sheet()
        self.office = make_office()
        self.registry = RegistryOffice(self.store, self.office, key=KEY)

    def tearDown(self):
        self.tmp.cleanup()

    def _counter(self):
        return Counter(self.counter_db, key=KEY, stock=STOCK)

    def test_sealed_package_full_flow_and_review(self):
        package = self.registry.seal_package(self.sheet, RECIPIENTS, STOCK)
        counter = self._counter()
        session = counter.receive_package(package)
        self.assertTrue(counter.verify_physicals(session)["ok"])
        result = counter.press(session)
        self.assertTrue(result["pressed"])

        # top slip torn away, machine stops before any receipt is registered
        first = counter.distribute(session)
        self.assertEqual(first["serial"], "XJ-01")
        counter.stop(session)

        # a relief clerk continues from the retained serials + action receipts
        relief = self._counter()
        resumed = relief.resume(session)
        self.assertEqual(resumed["pending_serials"], ["JZ-01"])
        second = relief.distribute(session)
        self.assertEqual(second["serial"], "JZ-01")
        r1 = relief.register_receipt(session, "XJ-01")
        r2 = relief.register_receipt(session, "JZ-01")
        self.assertEqual(relief.session_status(session)["status"], "COMPLETED")

        # receipts carry only what the recipient's own slip shows
        self.assertIn("涉限专业课", json.dumps(r1, ensure_ascii=False))
        self.assertNotIn("涉限", json.dumps(r2, ensure_ascii=False))

        # the authorized registry reviews which channels were blocked
        review = self.registry.review_press(relief, session)
        self.assertEqual(review["blocked_channels"], {"XJ-01": ["RED"], "JZ-01": []})
        self.assertEqual(
            review["actions"],
            ["received", "verify_physicals", "verify_physicals", "pressed",
             "distributed", "stopped", "resumed", "distributed",
             "receipt_registered", "receipt_registered", "completed"],
        )

    def test_no_second_press_for_the_same_sheet(self):
        package = self.registry.seal_package(self.sheet, RECIPIENTS, STOCK)
        counter = self._counter()
        session = counter.receive_package(package)
        counter.press(session)
        # the same package again: already consumed
        with self.assertRaises(PackageError):
            counter.receive_package(package)
        # a *different* package for the same sheet: still one press per sheet
        other = self.registry.seal_package(
            self.sheet, RECIPIENTS, STOCK, exclude_serials=["JZ-01"]
        )
        self.assertNotEqual(other["signature"], package["signature"])
        with self.assertRaises(CounterStateError):
            counter.receive_package(other)
        # and the consumed package cannot be pressed twice
        with self.assertRaises(CounterStateError):
            counter.press(session)

    def test_tampered_or_foreign_package_is_rejected(self):
        package = self.registry.seal_package(self.sheet, RECIPIENTS, STOCK)
        counter = self._counter()
        tampered = dict(package)
        tampered["stack"] = list(reversed(package["stack"]))
        with self.assertRaises(PackageError):
            counter.receive_package(tampered)
        foreign = Counter(self.counter_db + ".other", key=b"other-key", stock=STOCK)
        with self.assertRaises(PackageError):
            foreign.receive_package(package)

    def test_damaged_slip_voids_the_package_and_forces_replanning(self):
        package = self.registry.seal_package(self.sheet, RECIPIENTS, STOCK)
        self.assertEqual(package["stack"], ["XJ-01", "JZ-01"])
        counter = self._counter()
        session = counter.receive_package(package)
        outcome = counter.report_damaged(session, "JZ-01")
        self.assertTrue(outcome["void"])
        # the voided package may not be reused
        with self.assertRaises(PackageError):
            counter.receive_package(package)
        # the registry re-plans with the spare and seals a new package
        new_package = self.registry.reseal_after_damage(
            package, self.sheet, RECIPIENTS, STOCK, damaged_serial="JZ-01"
        )
        self.assertEqual(new_package["stack"], ["XJ-01", "JZ-02"])
        new_session = counter.receive_package(new_package)
        self.assertTrue(counter.press(new_session)["pressed"])

    def test_permit_mode_lets_the_counter_plan_itself(self):
        permit = self.registry.issue_permit(self.sheet)
        counter_office = office_from_permit(permit, key=KEY)
        plan = plan_press(counter_office, self.sheet, RECIPIENTS, STOCK)
        self.assertIsInstance(plan, PressPlan)
        self.assertEqual(plan.serials, ["XJ-01", "JZ-01"])
        self.assertEqual(plan.naming["LC_RES"].pair_id, "RES-R")
        # the permit carries the cross-row constraints, not just per-row lists
        self.assertIn("same_course_same_pair_across_rows", permit["constraints"])
        self.assertIn("no_cross_course_identical_pair", permit["constraints"])
        # a tampered permit does not verify
        forged = dict(permit)
        forged["courses"] = dict(permit["courses"])
        with self.assertRaises(PackageError):
            office_from_permit(forged, key=b"other-key")


if __name__ == "__main__":
    unittest.main()

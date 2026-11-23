"""Tests for the exchange-compatibility lab and the switch scheduler.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit import (
    Booklet,
    CompatibilityStore,
    audit_scope,
    check_compatibility,
    derive_constraints,
    enumerate_transcripts,
    parse_catalog,
    parse_requirements,
    parse_scope,
    parse_switch_problem,
    records_for_transcript,
    schedule,
    sign_channel_forms,
    sign_form,
    verify_witness,
)
from gradaudit.errors import SchemaError

MAT = os.path.join(ROOT, "materials", "725", "exchange")


def load(name):
    with open(os.path.join(MAT, name), encoding="utf-8") as fh:
        return json.load(fh)


def cat(name):
    return parse_catalog(load(name))


def scope(name="scope.json"):
    return parse_scope(load(name))


def reqs(name="requirements_G.json"):
    return [parse_requirements(load(name))]


def pu_old():
    return cat("pu_old.json")


def pu_new():
    return cat("pu_new.json")


def he_old():
    return cat("he_old.json")


def he_new():
    return cat("he_new.json")


def he_old_patched():
    return parse_catalog({
        "format": "course-catalog/1", "revision": 2,
        "courses": [{"course_id": "HE_G1", "categories": ["G"]}],
        "equivalences": {"X": ["HE_G1"], "Y": ["HE_G1"]},
    })


def booklet_both(req_list=None, sc=None):
    """Sign all four combos in both directions pu<->he."""
    req_list = req_list or reqs()
    sc = sc or scope()
    b = Booklet()
    for f in sign_channel_forms(
        sender_school="pu", receiver_school="he",
        sender_old=pu_old(), sender_new=pu_new(),
        receiver_old=he_old(), receiver_new=he_new(),
        requirements_list=req_list, scope=sc,
    ):
        b.add(f)
    for f in sign_channel_forms(
        sender_school="he", receiver_school="pu",
        sender_old=he_old(), sender_new=he_new(),
        receiver_old=pu_old(), receiver_new=pu_new(),
        requirements_list=req_list, scope=sc,
    ):
        b.add(f)
    return b


def problem(schools, channels, nights=2, capacity=None):
    doc = {
        "format": "switch-problem/1",
        "nights": nights,
        "schools": schools,
        "channels": channels,
    }
    if capacity is not None:
        doc["night_capacity"] = capacity
    return parse_switch_problem(doc)


# =========================================================================== #
# scope parsing / enumeration
# =========================================================================== #
class TestScope(unittest.TestCase):
    def test_tokens_and_enumeration_cover_scope(self):
        s = scope()
        self.assertEqual(s.tokens(), (("X", 1), ("Y", 1)))
        transcripts = enumerate_transcripts(s)
        # 0 records + 2 singletons (X1, Y1) + 1 double (X1,Y1) = 4
        self.assertIn((), transcripts)
        self.assertIn((("X", 1),), transcripts)
        self.assertIn((("Y", 1),), transcripts)
        self.assertIn((("X", 1), ("Y", 1)), transcripts)
        self.assertTrue(all(len(t) <= s.max_records for t in transcripts))

    def test_repeated_code_allowed(self):
        s = scope()
        transcripts = enumerate_transcripts(s)
        self.assertIn((("X", 1), ("X", 1)), transcripts)

    def test_scope_caps_enforced(self):
        with self.assertRaises(SchemaError):
            parse_scope({"format": "exchange-scope/1",
                         "external_courses": {"A": [1], "B": [1], "C": [1], "D": [1]},
                         "max_records": 1, "max_requirements": 1})
        with self.assertRaises(SchemaError):
            parse_scope({"format": "exchange-scope/1",
                         "external_courses": {"A": [1, 2, 3]},
                         "max_records": 1, "max_requirements": 1})
        with self.assertRaises(SchemaError):
            parse_scope({"format": "exchange-scope/1",
                         "external_courses": {"A": [1]},
                         "max_records": 5, "max_requirements": 1})
        with self.assertRaises(SchemaError):
            parse_scope({"format": "exchange-scope/1",
                         "external_courses": {"A": [1]},
                         "max_records": 1, "max_requirements": 3})

    def test_records_for_transcript_are_real(self):
        recs = records_for_transcript((("X", 1), ("Y", 1)))
        self.assertEqual(set(recs.records), {"R1", "R2"})
        self.assertEqual(
            {(r.external_course, r.points) for r in recs.records.values()},
            {("X", 1), ("Y", 1)},
        )


# =========================================================================== #
# universal compatibility testing
# =========================================================================== #
class TestCompatibility(unittest.TestCase):
    def test_old_old_is_compatible(self):
        res = check_compatibility(pu_old(), he_old(), reqs(), scope())
        self.assertTrue(res.compatible)
        self.assertIsNone(res.witness)

    def test_new_old_is_incompatible_with_y_one_witness(self):
        res = check_compatibility(pu_new(), he_old(), reqs(), scope())
        self.assertFalse(res.compatible)
        w = res.witness
        self.assertEqual(
            [(r["external_course"], r["points"]) for r in w["records"]],
            [("Y", 1)],
        )
        # sender has a legal allocation, receiver is globally infeasible
        self.assertTrue(w["sender"]["feasible"])
        self.assertFalse(w["receiver"]["feasible"])

    def test_witness_reverified_by_original_library(self):
        res = check_compatibility(pu_new(), he_old(), reqs(), scope())
        out = verify_witness(res.witness, pu_new(), he_old(), reqs()[0])
        self.assertTrue(out["witness_valid"])
        self.assertTrue(out["sender_accepted"])
        self.assertFalse(out["receiver_feasible"])

    def test_not_fooled_by_same_name_or_per_group_totals(self):
        # qing and lan agree that X and Y exist, but lan sends both codes to a
        # single shared course so the two groups compete for one record while
        # each group is reachable alone.
        s = cat("qing_catalog.json")
        r = cat("lan_catalog.json")
        sc = scope("scope_XY.json")
        rs = [parse_requirements(load("requirements_AB.json"))]
        res = check_compatibility(s, r, rs, sc)
        self.assertFalse(res.compatible)
        maxima = res.witness["receiver"]["group_max_ignoring_other_groups"]
        self.assertTrue(all(m["individually_reachable"] for m in maxima))
        self.assertTrue(verify_witness(res.witness, s, r, rs[0])["witness_valid"])

    def test_audit_scope_flags_violation(self):
        rows = audit_scope(pu_new(), he_old(), reqs(), scope())
        violations = [r for r in rows if r["violation"]]
        self.assertGreaterEqual(len(violations), 1)
        # every violation involves a Y record (the receiver's old catalog only
        # admits X), and the sender genuinely accepts each one
        for v in violations:
            self.assertTrue(v["sender_accepts"])
            self.assertFalse(v["receiver_accepts"])
            self.assertTrue(any(r["external_course"] == "Y" for r in v["transcript"]))
        good = [r for r in rows if not r["violation"]]
        self.assertTrue(good)

    def test_multiple_requirements_each_checked(self):
        sc = parse_scope({
            "format": "exchange-scope/1",
            "external_courses": {"X": [1], "Y": [1]},
            "max_records": 2, "max_requirements": 2,
        })
        rs = reqs() + reqs()
        res = check_compatibility(pu_old(), he_old(), rs, sc)
        self.assertEqual(len(res.per_requirement), 2)
        self.assertEqual(res.transcripts_in_scope, len(enumerate_transcripts(sc)))

    def test_requirements_beyond_scope_cap_rejected(self):
        rs = reqs() * 3
        with self.assertRaises(SchemaError):
            check_compatibility(pu_old(), he_old(), rs, scope())


# =========================================================================== #
# constraints derivation
# =========================================================================== #
class TestConstraints(unittest.TestCase):
    def _derive(self, channels):
        p = problem(
            [
                {"name": "pu", "old_catalog": pu_old().raw_document(),
                 "new_catalog": pu_new().raw_document()},
                {"name": "he", "old_catalog": he_old().raw_document(),
                 "new_catalog": he_new().raw_document()},
            ],
            channels,
        )
        return derive_constraints(p, booklet_both())

    def test_one_way_ordering(self):
        cons = self._derive([{"sender": "pu", "receiver": "he"}])
        ec = cons[("pu", "he")]
        # old,old ok; old,new ok; new,old broken; new,new ok
        self.assertTrue(ec.oo)
        self.assertTrue(ec.on)
        self.assertFalse(ec.no)
        self.assertTrue(ec.nn)
        self.assertEqual(ec.kind, "order_receiver_first")

    def test_reverse_channel_forces_same_night(self):
        cons = self._derive([
            {"sender": "pu", "receiver": "he"},
            {"sender": "he", "receiver": "pu"},
        ])
        # both directions break on the opposite-version combo -> co-switch
        self.assertEqual(cons[("pu", "he")].kind, "order_receiver_first")
        self.assertEqual(cons[("he", "pu")].kind, "order_receiver_first")
        # closing the cycle: each requires the other earlier -> same night

    def test_stale_form_rejected(self):
        # A form signed for a different catalog hash must not certify.
        p = problem(
            [
                {"name": "pu", "old_catalog": pu_old().raw_document(),
                 "new_catalog": pu_new().raw_document()},
                {"name": "he", "old_catalog": he_old().raw_document(),
                 "new_catalog": he_new().raw_document()},
            ],
            [{"sender": "pu", "receiver": "he"}],
        )
        # Swap pu_new for a *different* new catalog in the problem but keep
        # forms signed over the original.
        other_new = parse_catalog({
            "format": "course-catalog/1", "revision": 2,
            "courses": [{"course_id": "OTHER", "categories": ["G"]}],
            "equivalences": {"X": ["OTHER"]},
        })
        p.schools["pu"] = type(p.schools["pu"])(
            name="pu", old_catalog=pu_old(), new_catalog=other_new,
            allowed_nights=None,
        )
        cons = derive_constraints(p, booklet_both())
        self.assertIn(cons[("pu", "he")].statuses["new,old"], ("stale",))
        # new,new is also stale and therefore treated as not-compatible
        self.assertFalse(cons[("pu", "he")].nn)


# =========================================================================== #
# switch scheduling
# =========================================================================== #
class TestScheduler(unittest.TestCase):
    def _school(self, name, old, new, allowed=None):
        d = {"name": name, "old_catalog": old.raw_document(), "new_catalog": new.raw_document()}
        if allowed is not None:
            d["allowed_nights"] = allowed
        return d

    def test_pu_first_is_blocked_with_witness(self):
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [1]),
             self._school("he", he_old(), he_new(), [2])],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        res = schedule(p, booklet_both())
        self.assertFalse(res.feasible)
        blockers = res.diagnostics["blockers"]
        self.assertTrue(any(b["type"] == "order_deadline_conflict" for b in blockers))
        wit = next(b["witness"] for b in blockers if b.get("witness"))
        self.assertEqual([(r["external_course"], r["points"]) for r in wit["records"]],
                         [("Y", 1)])
        # witness really passes original-library re-check
        self.assertTrue(verify_witness(wit, pu_new(), he_old(), reqs()[0])["witness_valid"])

    def test_patching_receiver_old_catalog_restores_calendar(self):
        b = Booklet()
        for f in sign_channel_forms(
            sender_school="pu", receiver_school="he",
            sender_old=pu_old(), sender_new=pu_new(),
            receiver_old=he_old_patched(), receiver_new=he_new(),
            requirements_list=reqs(), scope=scope(),
        ):
            b.add(f)
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [1]),
             self._school("he", he_old_patched(), he_new(), [2])],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        res = schedule(p, b)
        self.assertTrue(res.feasible)
        self.assertEqual(res.switch_plan(), {"pu": 1, "he": 2})

    def test_swapping_nights_with_original_catalogs_works(self):
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [2]),
             self._school("he", he_old(), he_new(), [1])],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        res = schedule(p, booklet_both())
        self.assertTrue(res.feasible)
        self.assertEqual(res.switch_plan(), {"he": 1, "pu": 2})

    def test_reverse_channel_forces_same_night(self):
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [1]),
             self._school("he", he_old(), he_new(), [2])],
            [{"sender": "pu", "receiver": "he"},
             {"sender": "he", "receiver": "pu"}],
            capacity=1,
        )
        res = schedule(p, booklet_both())
        self.assertFalse(res.feasible)
        self.assertTrue(any(b["type"] == "co_switch_deadline_conflict"
                            for b in res.diagnostics["blockers"]))

    def test_reverse_channel_co_switch_feasible_when_shared_night_and_capacity(self):
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [2]),
             self._school("he", he_old(), he_new(), [2])],
            [{"sender": "pu", "receiver": "he"},
             {"sender": "he", "receiver": "pu"}],
            capacity=2,
        )
        res = schedule(p, booklet_both())
        self.assertTrue(res.feasible)
        self.assertEqual(res.switch_plan(), {"pu": 2, "he": 2})

    def test_broken_start_is_infeasible(self):
        he_broken = parse_catalog({
            "format": "course-catalog/1", "revision": 1,
            "courses": [{"course_id": "HB", "categories": ["G"]}],
            "equivalences": {"Q": ["HB"]},
        })
        b = Booklet()
        for f in sign_channel_forms(
            sender_school="pu", receiver_school="he",
            sender_old=pu_old(), sender_new=pu_new(),
            receiver_old=he_broken, receiver_new=he_new(),
            requirements_list=reqs(), scope=scope(),
        ):
            b.add(f)
        p = problem(
            [self._school("pu", pu_old(), pu_new()),
             self._school("he", he_broken, he_new())],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        res = schedule(p, b)
        self.assertFalse(res.feasible)
        self.assertEqual(res.diagnostics["reason"], "initial_state_incompatible")

    def test_capacity_deadline_conflict(self):
        # Three schools on two channels, all must switch by night 1 but only one
        # can switch per night -> capacity blocks.
        s = cat("pu_old.json")
        n = cat("pu_new.json")
        p = problem(
            [self._school("a", s, n, [1]),
             self._school("b", s, n, [1]),
             self._school("c", s, n, [2])],
            [],
            capacity=1,
        )
        # No channels at all: only capacity matters.
        res = schedule(p, Booklet())
        self.assertFalse(res.feasible)
        self.assertTrue(any(b["type"] == "capacity_deadline_conflict"
                            for b in res.diagnostics["blockers"]))

    def test_missing_form_is_reported(self):
        p = problem(
            [self._school("pu", pu_old(), pu_new()),
             self._school("he", he_old(), he_new())],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        res = schedule(p, Booklet())  # no forms at all
        self.assertFalse(res.feasible)
        # start morning needs (old,old); with no form it is 'missing' -> broken
        self.assertEqual(res.diagnostics["reason"], "initial_state_incompatible")
        # and it is surfaced as an evidence gap, not a proven break
        gaps = res.diagnostics.get("evidence_gaps", [])
        self.assertTrue(any(g["status"] == "missing" for g in gaps))

    def test_stale_form_is_evidence_gap_not_proven_break(self):
        # Sign forms over pu_new, then swap in a *different* new catalog; the
        # (new,*) combos become stale, i.e. unknown rather than proven broken.
        other_new = parse_catalog({
            "format": "course-catalog/1", "revision": 2,
            "courses": [{"course_id": "OTHER", "categories": ["G"]}],
            "equivalences": {"X": ["OTHER"], "Y": ["OTHER"]},
        })
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [1]),
             self._school("he", he_old(), he_new(), [2])],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        p.schools["pu"] = type(p.schools["pu"])(
            name="pu", old_catalog=pu_old(), new_catalog=other_new,
            allowed_nights=frozenset({1}),
        )
        res = schedule(p, booklet_both())
        # new,new stale -> final morning cannot be certified
        self.assertFalse(res.feasible)
        gaps = res.diagnostics.get("evidence_gaps", [])
        self.assertTrue(any(g["status"] == "stale" for g in gaps))

    def test_every_morning_in_calendar_is_checked(self):
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [2]),
             self._school("he", he_old(), he_new(), [1])],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        res = schedule(p, booklet_both())
        self.assertTrue(res.feasible)
        for batch in res.calendar:
            self.assertTrue(batch.channels_ok)
            for chk in batch.channel_checks:
                self.assertTrue(chk["compatible"])

    def test_schools_switch_exactly_once(self):
        p = problem(
            [self._school("pu", pu_old(), pu_new(), [2]),
             self._school("he", he_old(), he_new(), [1])],
            [{"sender": "pu", "receiver": "he"}],
            capacity=1,
        )
        res = schedule(p, booklet_both())
        switched = [n for b in res.calendar for n in b.switched]
        self.assertEqual(sorted(switched), ["he", "pu"])
        self.assertEqual(len(switched), len(set(switched)))


# =========================================================================== #
# signed form archive
# =========================================================================== #
class TestCompatibilityStore(unittest.TestCase):
    def test_save_and_reload_form(self):
        form = sign_form(
            pu_new(), he_old(), reqs(), scope(),
            sender_school="pu", receiver_school="he",
            sender_version="new", receiver_version="old",
        )
        self.assertFalse(form.compatible)
        with CompatibilityStore(":memory:") as cs:
            h = cs.save(form)
            loaded = cs.get_form(h)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.content_hash(), h)
            self.assertFalse(loaded.compatible)

    def test_find_forms(self):
        with CompatibilityStore(":memory:") as cs:
            for f in sign_channel_forms(
                sender_school="pu", receiver_school="he",
                sender_old=pu_old(), sender_new=pu_new(),
                receiver_old=he_old(), receiver_new=he_new(),
                requirements_list=reqs(), scope=scope(),
            ):
                cs.save(f)
            got = cs.find_forms(sender_school="pu", receiver_school="he")
            self.assertEqual(len(got), 4)

    def test_form_hash_is_tamper_evident(self):
        form = sign_form(
            pu_old(), he_old(), reqs(), scope(),
            sender_school="pu", receiver_school="he",
            sender_version="old", receiver_version="old",
        )
        doc = form.as_document()
        self.assertTrue(doc["compatible"])
        doc["compatible"] = False  # tamper
        from gradaudit.model import content_hash
        doc.pop("signed_at", None)
        self.assertNotEqual(content_hash(doc), form.content_hash())


# =========================================================================== #
# randomized cross-check: exhaustive lab vs a naive per-transcript oracle
# =========================================================================== #
class TestLabCrossCheck(unittest.TestCase):
    def test_random_small_channels(self):
        rng = random.Random(4242)
        cats = ["A", "B"]
        for trial in range(60):
            def rand_catalog(rev):
                courses = []
                for i in range(rng.randint(1, 3)):
                    chosen = rng.sample(cats, rng.randint(1, 2))
                    courses.append({"course_id": f"C{i}", "categories": chosen})
                cids = [c["course_id"] for c in courses]
                equiv = {}
                for ext in ("X", "Y"):
                    if rng.random() < 0.8:
                        equiv[ext] = rng.sample(cids, rng.randint(1, len(cids)))
                return parse_catalog({
                    "format": "course-catalog/1", "revision": rev,
                    "courses": courses, "equivalences": equiv,
                })

            sender = rand_catalog(1)
            receiver = rand_catalog(1)
            req = parse_requirements({
                "request_id": "Q", "revision": 1,
                "groups": [
                    {"group_id": "GA", "allowed_categories": ["A"],
                     "minimum_points": rng.randint(1, 3)},
                ],
            })
            sc = parse_scope({
                "format": "exchange-scope/1",
                "external_courses": {"X": [1], "Y": [1]},
                "max_records": 2, "max_requirements": 1,
            })
            res = check_compatibility(sender, receiver, [req], sc)
            # naive oracle: enumerate directly and use the solver
            from gradaudit.solver import solve
            expected_ok = True
            for t in enumerate_transcripts(sc):
                recs = records_for_transcript(t)
                if solve(sender, recs, req).feasible and not solve(receiver, recs, req).feasible:
                    expected_ok = False
                    break
            with self.subTest(trial=trial):
                self.assertEqual(res.compatible, expected_ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)

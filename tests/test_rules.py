"""Tests for the rule-realization subsystem.

Covers every caution the brief raises:

* one contingent script for the whole table, never a stitched best-per-book plan;
* separation must be real (indistinguishable volumes, budget exhaustion);
* a trial step can permanently merge candidates;
* the erratum that makes 丙/丁 identical;
* a broken book never enters the candidate set;
* stale translated blocks are refused and must be re-identified;
* the translator's seat reads patterns + fired clause + state from a key string;
* merged and independent galleries agree and both verify end to end.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from delivery_system import rule_materials as rm
from delivery_system.delivery_repository import DeliveryRepository
from delivery_system.errors import TranslationExists
from delivery_system.models import RuleVocabulary
from delivery_system.plan import (
    Leaf,
    Node,
    build_handoff,
    run_script,
    screen_candidates,
    synthesize,
    synthesize_merged,
    verify_plan,
)
from delivery_system.rule_materials import (
    StaleBlockError,
    check_blocks_fresh,
    volume_from_dict,
    volume_to_dict,
)
from delivery_system.rules import (
    Clause,
    Volume,
    build_machine,
    check_completeness,
    reachable_states,
    replay,
    step,
)
from delivery_system.source_catalog import PublishedSourceReader, SourceCatalog


class RulesEngineTests(unittest.TestCase):
    def test_first_applicable_clause_in_block_order_wins(self):
        # Two clauses both apply; the earlier block's clause must fire.
        early = Clause("A", 1, "k", pattern="first")
        late = Clause("B", 1, "k", pattern="second")
        volume = Volume(
            volume_id="V",
            blocks=(("A", "B"),),
            flags=(),
            visible_flags=frozenset(),
            keys=("k",),
            initial_state=frozenset(),
            clauses=(early, late),
        )
        self.assertEqual(step(volume, frozenset(), "k").pattern, "first")

    def test_rewrite_reads_pre_key_state(self):
        # A clause requiring x sets x and y; the next key sees both.
        volume = Volume(
            volume_id="V",
            blocks=(("A",),),
            flags=("x", "y"),
            visible_flags=frozenset({"y"}),
            keys=("k",),
            initial_state=frozenset(),
            clauses=(
                Clause("A", 1, "k", rewrite=(("x", True), ("y", True)), pattern="p"),
            ),
        )
        first = step(volume, frozenset(), "k")
        self.assertEqual(first.pattern, "p")
        self.assertEqual(first.state, frozenset({"x", "y"}))

    def test_white_clause_explicit_and_gap_detected(self):
        # Missing clause at a reachable state is a real gap, not a hidden white.
        volume = Volume(
            volume_id="V",
            blocks=(("A",),),
            flags=(),
            visible_flags=frozenset(),
            keys=("k", "j"),
            initial_state=frozenset(),
            clauses=(Clause("A", 1, "k", pattern="p"),),
        )
        gaps = check_completeness(volume)
        self.assertTrue(any(g.key == "j" for g in gaps))
        # And the gap carries a walkable history.
        gap = next(g for g in gaps if g.key == "j")
        self.assertEqual(gap.keys, ("j",))

    def test_lock_answers_every_key_and_is_not_a_gap(self):
        # A lock trap answers white forever, so it must not be reported as a gap.
        volume = rm.make_book("甲", rm.RED, rm.SQUARE)
        started = step(volume, frozenset(), rm.KEY_TURN)  # wrong opening key
        self.assertEqual(started.pattern, rm.WHITE)
        for key in volume.keys:
            self.assertEqual(step(volume, started.state, key).pattern, rm.WHITE)
        self.assertFalse(check_completeness(volume))

    def test_replay_reports_clause_and_state(self):
        volume = rm.make_book("甲", rm.RED, rm.SQUARE)
        trace = replay(volume, [rm.KEY_COLOR, rm.KEY_TURN])
        self.assertEqual(trace.patterns, (rm.RED, rm.SQUARE))
        self.assertTrue(trace.complete)
        self.assertEqual(trace.steps[0].clause.key, rm.KEY_COLOR)
        self.assertEqual(trace.steps[1].clause.key, rm.KEY_TURN)
        # Reading the mark locks the box: the reserved lock flag is now set.
        self.assertEqual(trace.final_state, frozenset({"!locked"}))

    def test_clause_revision_conflict_rejected(self):
        with self.assertRaises(Exception):
            Volume(
                volume_id="V",
                blocks=(("A",),),
                flags=(),
                visible_flags=frozenset(),
                keys=("k",),
                initial_state=frozenset(),
                clauses=(
                    Clause("A", 1, "k", pattern="p"),
                    Clause("A", 2, "k", pattern="q"),
                ),
            )

    def test_unknown_flag_rejected(self):
        with self.assertRaises(Exception):
            Volume(
                volume_id="V",
                blocks=(("A",),),
                flags=(),
                visible_flags=frozenset(),
                keys=("k",),
                initial_state=frozenset(),
                clauses=(Clause("A", 1, "k", requires_on=frozenset({"nope"})),),
            )


class BaseFourTests(unittest.TestCase):
    def test_base_plan_two_steps_colour_then_mark(self):
        plan = synthesize(rm.base_four())
        self.assertTrue(plan.ok)
        self.assertEqual(plan.depth, 2)
        self.assertFalse(plan.verification_problems)
        self.assertIsInstance(plan.strategy, Node)
        self.assertEqual(plan.strategy.key, rm.KEY_COLOR)

    def test_each_book_report_correct(self):
        plan = synthesize(rm.base_four())
        for volume in rm.base_four():
            trace = run_script(plan.strategy, build_machine(volume))
            self.assertEqual(trace.reported, volume.volume_id, trace.to_dict())

    def test_no_stitched_best_plan(self):
        # The single script's first key must be the same for every book.
        plan = synthesize(rm.base_four())
        self.assertEqual(plan.strategy.key, rm.KEY_COLOR)
        # Its two branches use different second keys per colour.
        red = plan.strategy.branch(rm.RED)
        blue = plan.strategy.branch(rm.BLUE)
        self.assertEqual(red.key, rm.KEY_TURN)
        self.assertEqual(blue.key, rm.KEY_PUSH)

    def test_merged_and_independent_agree(self):
        a = synthesize(rm.base_four())
        b = synthesize_merged(rm.base_four())
        self.assertEqual(a.depth, b.depth)
        self.assertEqual(a.strategy, b.strategy)


class HandoffTests(unittest.TestCase):
    def test_handoff_has_complete_walk_proof(self):
        handoff = build_handoff(synthesize(rm.base_four()))
        self.assertTrue(handoff["deliverable"])
        self.assertEqual(set(handoff["walk_proof"]), {"甲", "乙", "丙", "丁"})
        for vid, proof in handoff["walk_proof"].items():
            self.assertEqual(proof["reports"], vid)
            self.assertEqual(len(proof["keys"]), len(proof["patterns"]))
            self.assertTrue(proof["ends_locked"])
        self.assertFalse(handoff["verification_problems"])

    def test_handoff_impossibility_carries_certificate(self):
        handoff = build_handoff(synthesize(rm.ding_erratum()))
        self.assertFalse(handoff["deliverable"])
        self.assertEqual(handoff["impossibility"]["reason"], "indistinguishable_volumes")


class ImpossibilityTests(unittest.TestCase):
    def test_ding_erratum_makes_bing_ding_identical(self):
        plan = synthesize(rm.ding_erratum())
        self.assertFalse(plan.ok)
        self.assertEqual(plan.failure["reason"], "indistinguishable_volumes")
        groups = [sorted(g) for g in plan.failure["indistinguishable_groups"]]
        self.assertIn(["丁", "丙"], groups)

    def test_budget_one_no_script_but_all_legal(self):
        plan = synthesize(rm.base_four(), max_steps=1)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.failure["reason"], "budget_exhausted")
        # all four are still individually complete
        for volume in rm.base_four():
            self.assertFalse(check_completeness(volume))
        # the certificate names a real first-key branch left unresolved
        color_entry = next(
            e for e in plan.failure["per_key"] if e["key"] == rm.KEY_COLOR
        )
        self.assertTrue(color_entry["blocked_branches"])
        self.assertEqual(len(color_entry["observations"]), 2)  # 红 / 蓝

    def test_insufficient_budget_failure_is_walkable_explanation(self):
        plan = synthesize(rm.base_four(), max_steps=1)
        # The blocked branch must list the still-merged candidates.
        color_entry = next(
            e for e in plan.failure["per_key"] if e["key"] == rm.KEY_COLOR
        )
        merged = {tuple(sorted(b["remaining"])) for b in color_entry["blocked_branches"]}
        self.assertIn(("乙", "甲"), merged)
        self.assertIn(("丁", "丙"), merged)

    def test_exclusive_first_key_needs_adaptive_second_key(self):
        plan = synthesize(rm.exclusive_first_key())
        self.assertTrue(plan.ok)
        self.assertEqual(plan.depth, 2)
        # No single first key separates all three.
        for key in ("x", "y"):
            buckets: dict[str, list[str]] = {}
            for volume in rm.exclusive_first_key():
                buckets.setdefault(replay(volume, [key]).patterns[0], []).append(
                    volume.volume_id
                )
            self.assertTrue(any(len(v) > 1 for v in buckets.values()))


class TrialMergeTests(unittest.TestCase):
    def test_trap_separable_by_turn(self):
        plan = synthesize(rm.trial_trap_pair())
        self.assertTrue(plan.ok)
        self.assertEqual(plan.depth, 2)

    def test_wrong_trial_permanently_merges(self):
        a, b = rm.trial_trap_pair()
        ra = replay(a, [rm.KEY_COLOR, rm.KEY_PUSH, rm.KEY_TURN])
        rb = replay(b, [rm.KEY_COLOR, rm.KEY_PUSH, rm.KEY_TURN])
        self.assertEqual(ra.patterns, rb.patterns)
        self.assertEqual(ra.patterns, (rm.RED, rm.WHITE, rm.WHITE))
        # In this merged state no script can ever separate them.
        merged_plan = synthesize(rm.trial_trap_pair(), max_steps=1)
        self.assertFalse(merged_plan.ok)


class HiddenStateTests(unittest.TestCase):
    def test_hidden_pair_needs_three_steps(self):
        plan = synthesize(rm.hidden_pair())
        self.assertTrue(plan.ok)
        self.assertEqual(plan.depth, 3)
        for walk in plan.walks.values():
            self.assertEqual(list(walk.patterns[:2]), [rm.RED, rm.SQUARE])
        last = {walk.patterns[-1] for walk in plan.walks.values()}
        self.assertEqual(last, {rm.STAR, rm.MOON})


class ScreeningTests(unittest.TestCase):
    def test_broken_book_rejected_with_reason(self):
        accepted, rejected = screen_candidates(rm.base_four() + [rm.broken_book()])
        self.assertEqual([v.volume_id for v in accepted], ["甲", "乙", "丙", "丁"])
        self.assertEqual([r.volume_id for r in rejected], ["庚"])
        self.assertEqual(rejected[0].reason, "missing_answer")
        # The rejection carries concrete walkable examples.
        self.assertGreaterEqual(rejected[0].detail["count"], 1)

    def test_rejected_book_never_enters_candidates(self):
        plan = synthesize(rm.base_four() + [rm.broken_book()])
        self.assertTrue(plan.ok)
        self.assertNotIn("庚", plan.candidates)
        self.assertEqual(plan.depth, 2)

    def test_too_many_volumes_refused(self):
        many = [rm.make_book(f"V{i}", rm.RED, rm.SQUARE) for i in range(9)]
        plan = synthesize(many)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.failure["reason"], "too_many_candidates")


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.source_db = os.path.join(self.tmp, "src.db")
        self.delivery_db = os.path.join(self.tmp, "del.db")
        self.catalog = SourceCatalog(self.source_db)
        self.repo = DeliveryRepository(self.delivery_db, PublishedSourceReader(self.source_db))
        self.catalog.create_document(
            "RULES",
            [
                {"segment_id": "开局", "revision": 1, "text": "opening"},
                {"segment_id": "辨纹", "revision": 1, "text": "mark"},
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_vocabulary_bound_to_revision(self):
        vocab = RuleVocabulary(
            flags=("red", "blue"),
            visible_flags=frozenset({"red", "blue"}),
            patterns=(rm.RED, rm.BLUE, rm.SQUARE, rm.CIRCLE, rm.STAR, rm.MOON, rm.WHITE),
            keys=rm.KEYS,
        )
        self.catalog.set_segment_vocabulary("RULES", "开局", 1, vocab)
        got = self.catalog.get_segment_vocabulary("RULES", "开局")
        self.assertEqual(got, vocab)
        # After a revision, the old revision's vocabulary is still queryable by
        # revision, but registering for the old revision against the new one is
        # refused.
        self.catalog.revise_segment("RULES", "开局", "new", revision=2)
        self.assertEqual(self.catalog.get_segment_vocabulary("RULES", "开局", 1), vocab)
        with self.assertRaises(Exception):
            self.catalog.set_segment_vocabulary("RULES", "开局", 1, vocab)

    def test_stale_block_refused_after_revision(self):
        base = rm.base_four()
        self.catalog.revise_segment("RULES", "辨纹", "mark v2", revision=2)
        snapshot = self.catalog.published_product("RULES").revision_map
        for volume in base:
            with self.assertRaises(StaleBlockError):
                check_blocks_fresh(volume, snapshot)

    def test_co_translated_block_rebuilds_one_volume(self):
        base = rm.base_four()
        for volume in base:
            self.repo.add_rule_volume(
                volume.volume_id, "RULES", json.dumps(volume_to_dict(volume), ensure_ascii=False)
            )
        # Revise the mark block and re-translate only 丁.
        self.catalog.revise_segment("RULES", "辨纹", "mark v2", revision=2)
        new_ding = rm.make_book("丁", rm.BLUE, rm.MOON, revisions={"辨纹": 2, "开局": 1})
        check_blocks_fresh(new_ding, self.catalog.published_product("RULES").revision_map)
        self.repo.add_rule_volume(
            "丁", "RULES", json.dumps(volume_to_dict(new_ding), ensure_ascii=False), replace=True
        )
        # 甲 still frozen at rev1, untouched.
        jia = volume_from_dict(self.repo.get_rule_volume("甲"))
        self.assertEqual(jia.revision_base(), {"开局": 1, "辨纹": 1})
        self.assertEqual(
            [v["volume_id"] for v in self.repo.list_rule_volumes("RULES")],
            ["丁", "丙", "乙", "甲"],
        )

    def test_replacement_without_flag_is_refused(self):
        jia = rm.base_four()[0]
        self.repo.add_rule_volume(
            "甲", "RULES", json.dumps(volume_to_dict(jia), ensure_ascii=False)
        )
        altered = rm.make_book("甲", rm.RED, rm.CIRCLE)
        with self.assertRaises(TranslationExists):
            self.repo.add_rule_volume(
                "甲", "RULES", json.dumps(volume_to_dict(altered), ensure_ascii=False)
            )

    def test_old_volume_replays_old_rules(self):
        jia = rm.base_four()[0]
        self.repo.add_rule_volume(
            "甲", "RULES", json.dumps(volume_to_dict(jia), ensure_ascii=False)
        )
        self.catalog.revise_segment("RULES", "辨纹", "mark v2", revision=2)
        old = volume_from_dict(self.repo.get_rule_volume("甲"))
        trace = replay(old, [rm.KEY_COLOR, rm.KEY_TURN])
        self.assertEqual(trace.patterns, (rm.RED, rm.SQUARE))
        self.assertTrue(trace.complete)


class JSONRoundTripTests(unittest.TestCase):
    def test_volume_roundtrip(self):
        for volume in rm.base_four() + rm.hidden_pair() + rm.exclusive_first_key():
            restored = volume_from_dict(volume_to_dict(volume))
            self.assertEqual(restored.volume_id, volume.volume_id)
            self.assertEqual(restored.keys, volume.keys)
            self.assertEqual(len(restored.clauses), len(volume.clauses))
            # Behaviour preserved.
            for key in volume.keys:
                self.assertEqual(
                    step(restored, restored.initial_state, key).pattern,
                    step(volume, volume.initial_state, key).pattern,
                )


if __name__ == "__main__":
    unittest.main()

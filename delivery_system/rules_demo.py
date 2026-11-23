"""End-to-end rule-realization demo covering every scenario in the brief.

Runs against in-memory volume data (also serialised under materials/rules) and
persists volumes through the delivery repository to prove the release-bound
assembly and single-volume erratum paths.

Each scenario asserts a concrete outcome:

* ``base_four``           – 甲乙丙丁, two steps suffice (colour then mark).
* ``ding_erratum``        – 丁's end rule changed; 丙/丁 become indistinguishable.
* ``base_budget_one``     – the same four books, budget cut to one step: every
  book is still legal, but no script can be signed.
* ``hidden_pair``         – identical first two prints, different internal
  state; separation only on the third press.
* ``trial_trap``          – separable on 转 but a 推 trial permanently merges.
* ``exclusive_first``     – pairwise distinguishable, no single first key
  splits all three.
* ``broken_book``         – one promised answer missing; the book is rejected
  before it can enter the candidate set.
* ``assembly``            – a stale translated block is refused after a source
  revision; a co-translated new block rebuilds one volume without touching the
  others; an old package still replays by its old rules.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from . import rule_materials as rm
from .delivery_repository import DeliveryRepository
from .errors import TranslationExists
from .plan import (
    PlanResult,
    build_handoff,
    run_script,
    screen_candidates,
    synthesize,
    synthesize_merged,
)
from .rule_materials import (
    StaleBlockError,
    check_blocks_fresh,
    volume_from_dict,
    volume_to_dict,
)
from .rules import build_machine, check_completeness, replay
from .source_catalog import PublishedSourceReader, SourceCatalog


def _summarise(result: PlanResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "depth": result.depth,
        "candidates": result.candidates,
        "rejected": [r.to_dict() for r in result.rejected],
        "failure": result.failure,
        "verification_problems": result.verification_problems,
        "walks": {vid: w.to_dict() for vid, w in result.walks.items()},
    }


def run_rules_demo(out_dir: str, materials: Optional[str] = None) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    report: dict[str, Any] = {"ok": True, "scenarios": {}, "checks": []}

    def check(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            report["ok"] = False

    def scenario(name: str) -> dict[str, Any]:
        block: dict[str, Any] = {}
        report["scenarios"][name] = block
        return block

    # -- base four -----------------------------------------------------
    base = rm.base_four()
    base_plan = synthesize(base)
    block = scenario("base_four")
    block.update(_summarise(base_plan))
    check("base_two_steps", base_plan.ok and base_plan.depth == 2, base_plan.depth)
    check("base_no_problems", not base_plan.verification_problems)
    check(
        "base_first_key_colour",
        isinstance(base_plan.strategy, object) and base_plan.strategy.key == rm.KEY_COLOR,  # type: ignore[union-attr]
    )
    expected_walks = {
        "甲": ([rm.KEY_COLOR, rm.KEY_TURN], [rm.RED, rm.SQUARE]),
        "乙": ([rm.KEY_COLOR, rm.KEY_TURN], [rm.RED, rm.CIRCLE]),
        "丙": ([rm.KEY_COLOR, rm.KEY_PUSH], [rm.BLUE, rm.STAR]),
        "丁": ([rm.KEY_COLOR, rm.KEY_PUSH], [rm.BLUE, rm.MOON]),
    }
    for vid, (keys, patterns) in expected_walks.items():
        walk = base_plan.walks[vid]
        check(f"base_walk_{vid}", list(walk.keys) == keys and list(walk.patterns) == patterns,
              walk.to_dict())

    # merged strategy must agree with the independent search
    merged = synthesize_merged(base)
    check("base_merged_same_depth", merged.depth == base_plan.depth, merged.depth)
    check("base_merged_same_strategy", merged.strategy == base_plan.strategy)

    # live: run the script against a real 丁 box, patterns only
    trace = run_script(base_plan.strategy, build_machine(base[3]))
    check("base_live_reports_ding", trace.reported == "丁", trace.to_dict())

    # every key answered at every reachable configuration
    for volume in base:
        check(f"base_complete_{volume.volume_id}", not check_completeness(volume))

    # -- ding erratum --------------------------------------------------
    erratum = rm.ding_erratum()
    erratum_plan = synthesize(erratum)
    block = scenario("ding_erratum")
    block.update(_summarise(erratum_plan))
    check("erratum_impossible", not erratum_plan.ok)
    check(
        "erratum_indistinguishable",
        erratum_plan.failure is not None
        and erratum_plan.failure.get("reason") == "indistinguishable_volumes"
        and ["丁", "丙"] in [sorted(g) for g in erratum_plan.failure.get("indistinguishable_groups", [])],
        erratum_plan.failure,
    )

    # -- budget one ----------------------------------------------------
    budget_plan = synthesize(base, max_steps=1)
    block = scenario("base_budget_one")
    block.update(_summarise(budget_plan))
    check("budget_one_no_script", not budget_plan.ok)
    check(
        "budget_one_reason",
        budget_plan.failure is not None
        and budget_plan.failure.get("reason") == "budget_exhausted",
        budget_plan.failure,
    )
    # All four remain individually legal (complete, no gaps).
    for volume in base:
        check(f"budget_one_legal_{volume.volume_id}", not check_completeness(volume))
    # The certificate must show a *reachable* blocked branch for the first key.
    first_key_blocks = []
    if budget_plan.failure:
        for entry in budget_plan.failure.get("per_key", []):  # type: ignore[union-attr]
            if entry["key"] == rm.KEY_COLOR:
                first_key_blocks = entry["blocked_branches"]
    check("budget_one_blocked_branch", bool(first_key_blocks), first_key_blocks)

    # -- hidden pair ---------------------------------------------------
    hidden = rm.hidden_pair()
    hidden_plan = synthesize(hidden)
    block = scenario("hidden_pair")
    block.update(_summarise(hidden_plan))
    check("hidden_three_steps", hidden_plan.ok and hidden_plan.depth == 3, hidden_plan.depth)
    for vid, walk in hidden_plan.walks.items():
        check(f"hidden_same_prefix_{vid}", list(walk.patterns[:2]) == [rm.RED, rm.SQUARE],
              walk.to_dict())
    check(
        "hidden_split_on_third",
        {w.patterns[-1] for w in hidden_plan.walks.values()} == {rm.STAR, rm.MOON},
        {vid: list(w.patterns) for vid, w in hidden_plan.walks.items()},
    )

    # -- trial trap ----------------------------------------------------
    trap = rm.trial_trap_pair()
    trap_plan = synthesize(trap)
    block = scenario("trial_trap")
    block.update(_summarise(trap_plan))
    check("trap_separable_on_turn", trap_plan.ok and trap_plan.depth == 2, trap_plan.depth)
    # Pressing 推 after 红 blanks *both* books identically: irrecoverable.
    for volume in trap:
        r = replay(volume, [rm.KEY_COLOR, rm.KEY_PUSH])
        check(
            f"trap_pu_blanks_{volume.volume_id}",
            list(r.patterns) == [rm.RED, rm.WHITE],
            r.to_dict(),
        )
    r_a = replay(trap[0], [rm.KEY_COLOR, rm.KEY_PUSH, rm.KEY_TURN])
    r_b = replay(trap[1], [rm.KEY_COLOR, rm.KEY_PUSH, rm.KEY_TURN])
    check("trap_permanent_merge", r_a.patterns == r_b.patterns, [r_a.patterns, r_b.patterns])

    # -- exclusive first key -------------------------------------------
    excl = rm.exclusive_first_key()
    excl_plan = synthesize(excl)
    block = scenario("exclusive_first")
    block.update(_summarise(excl_plan))
    check("excl_two_steps", excl_plan.ok and excl_plan.depth == 2, excl_plan.depth)
    # Neither lone first key separates all three.
    for key in ("x", "y"):
        patterns = {}
        for volume in excl:
            patterns.setdefault(replay(volume, [key]).patterns[0], []).append(volume.volume_id)
        check(f"excl_{key}_not_total", any(len(v) > 1 for v in patterns.values()), patterns)

    # -- broken book ---------------------------------------------------
    with_broken = rm.base_four() + [rm.broken_book()]
    accepted, rejected = screen_candidates(with_broken)
    block = scenario("broken_book")
    block["accepted"] = [v.volume_id for v in accepted]
    block["rejected"] = [r.to_dict() for r in rejected]
    check("broken_rejected", [r.volume_id for r in rejected] == ["庚"],
          [r.to_dict() for r in rejected])
    broken_gaps = check_completeness(rm.broken_book())
    check("broken_has_gap", bool(broken_gaps), [g.to_dict() for g in broken_gaps])
    # The gap comes with a walkable history ending in the missing key.
    if broken_gaps:
        gap = broken_gaps[0]
        walked = replay(rm.broken_book(), list(gap.keys))
        check(
            "broken_gap_walkable",
            walked.keys[-1] == gap.key and not walked.complete,
            gap.to_dict(),
        )
    # Planning over base+broken must behave exactly like base: the broken book
    # never becomes a candidate.
    plan_with_broken = synthesize(with_broken)
    check(
        "broken_not_in_candidates",
        plan_with_broken.candidates == base_plan.candidates,
        plan_with_broken.candidates,
    )

    # -- release-bound assembly / erratum / old-package replay ----------
    _assembly_scenario(out_dir, report, check, scenario)

    # -- hand-off document (script + complete walk proof) ---------------
    handoff = build_handoff(base_plan)
    block = scenario("handoff")
    block["deliverable"] = handoff["deliverable"]
    block["depth"] = handoff["depth"]
    check("handoff_deliverable", handoff["deliverable"])
    check(
        "handoff_proof_books",
        set(handoff["walk_proof"]) == {"甲", "乙", "丙", "丁"},
        list(handoff["walk_proof"]),
    )
    check(
        "handoff_each_reports_self",
        all(proof["reports"] == vid for vid, proof in handoff["walk_proof"].items()),
    )
    impossibility = build_handoff(synthesize(rm.ding_erratum()))
    check(
        "handoff_impossibility_certificate",
        not impossibility["deliverable"]
        and impossibility["impossibility"]["reason"] == "indistinguishable_volumes",
        impossibility.get("impossibility", {}),
    )

    return report


def _assembly_scenario(out_dir, report, check, scenario) -> None:
    """A source revision, stale-block refusal, single-volume erratum, old replay."""

    source_db = os.path.join(out_dir, "rules_source.db")
    delivery_db = os.path.join(out_dir, "rules_delivery.db")
    catalog = SourceCatalog(source_db)
    repo = DeliveryRepository(delivery_db, PublishedSourceReader(source_db))

    document_id = "RULES"
    catalog.create_document(
        document_id,
        [
            {"segment_id": "开局", "revision": 1, "text": "opening"},
            {"segment_id": "辨纹", "revision": 1, "text": "mark"},
        ],
    )
    from .models import RuleVocabulary

    vocab = RuleVocabulary(
        flags=("red", "blue"),
        visible_flags=frozenset({"red", "blue"}),
        patterns=(rm.RED, rm.BLUE, rm.SQUARE, rm.CIRCLE, rm.STAR, rm.MOON, rm.WHITE),
        keys=rm.KEYS,
    )
    catalog.set_segment_vocabulary(document_id, "开局", 1, vocab)
    catalog.set_segment_vocabulary(document_id, "辨纹", 1, vocab)

    base = rm.base_four()
    for volume in base:
        check_blocks_fresh(volume, catalog.published_product(document_id).revision_map)
        repo.add_rule_volume(volume.volume_id, document_id, json.dumps(volume_to_dict(volume), ensure_ascii=False))

    block = scenario("assembly")

    # Revise 辨纹 -> revision 2: old volumes become stale.
    catalog.revise_segment(document_id, "辨纹", "mark revised", revision=2)
    snapshot = catalog.published_product(document_id).revision_map
    stale_refused: list[str] = []
    for volume in base:
        try:
            check_blocks_fresh(volume, snapshot)
        except StaleBlockError:
            stale_refused.append(volume.volume_id)
    check("assembly_all_stale", sorted(stale_refused) == sorted(v.volume_id for v in base),
          stale_refused)

    # Re-translate only 丁 at the new revision; 甲/乙/丙 remain at revision 1.
    new_ding = rm.make_book(
        "丁", rm.BLUE, rm.MOON, revisions={"辨纹": 2, "开局": 1}
    )
    other_snapshot = {"开局": 1, "辨纹": 1}
    for volume in base[:3]:
        check_blocks_fresh(volume, other_snapshot)  # still valid against rev1 segments
    check_blocks_fresh(new_ding, snapshot)
    ding_json = json.dumps(volume_to_dict(new_ding), ensure_ascii=False)
    repo.add_rule_volume("丁", document_id, ding_json, replace=True)
    stored = repo.get_rule_volume("丁")
    check("assembly_ding_replaced", volume_from_dict(stored).volume_id == "丁")
    # 甲 remains the frozen rev1 volume, untouched by the ding erratum.
    check(
        "assembly_others_untouched",
        volume_from_dict(repo.get_rule_volume("甲")).revision_base() == {"开局": 1, "辨纹": 1},
    )

    # Re-adding a *changed* volume without replace=True is refused.
    blocked = False
    try:
        altered = rm.make_book("丁", rm.BLUE, rm.STAR)
        repo.add_rule_volume(
            "丁", document_id, json.dumps(volume_to_dict(altered), ensure_ascii=False)
        )
    except TranslationExists:
        blocked = True
    check("assembly_immutable_without_replace", blocked)

    block["stale_refused"] = stale_refused
    block["stored_volumes"] = [v["volume_id"] for v in repo.list_rule_volumes(document_id)]

    # Old auditorium keeps replaying the old rules: the frozen rev1 甲 still
    # answers 试色/转 exactly as before, even after the source moved to rev2.
    old_jia = volume_from_dict(repo.get_rule_volume("甲"))
    old_replay = replay(old_jia, [rm.KEY_COLOR, rm.KEY_TURN])
    check(
        "assembly_old_replay_old_rules",
        list(old_replay.patterns) == [rm.RED, rm.SQUARE] and old_replay.complete,
        old_replay.to_dict(),
    )


def run(out_dir: str) -> dict[str, Any]:
    return run_rules_demo(out_dir)

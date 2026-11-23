#!/usr/bin/env python3
"""End-to-end demonstration of the exchange-compatibility lab and switch scheduler.

Uses the small consortium materials in ``materials/725/exchange``.  Shows, in
order:

  1. one directed channel (Pu -> He) certified at old/old by *exhaustive*
     universal enumeration (no same-name guessing, no per-group shortcut);
  2. the "Pu new -> He old" pair refuted with a real witness (a Y, 1-point
     record that Pu accepts and He's old catalog cannot catch), re-checked by
     the original library;
  3. a story where same-named codes exist and each group is reachable alone,
     yet a single record must serve two groups -> refuted with a witness;
  4. the switch puzzle: Pu must switch night 1, He night 2, one school per
     night -> blocked by an ordering deadline, with the witness attached;
  5. patching He's old catalog through a formal revision restores the calendar;
  6. keeping the original four catalogs but swapping the allowed nights
     (He first, Pu second) also gives a calendar;
  7. a reverse channel that forces Pu and He onto the same night (co-switch);
  8. signed compatibility forms archived and re-read before the switch window.

Usage:  PYTHONPATH=. python3 examples/exchange_demo.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gradaudit import (
    Booklet,
    CompatibilityStore,
    check_compatibility,
    derive_constraints,
    enumerate_transcripts,
    parse_catalog,
    parse_requirements,
    parse_scope,
    parse_switch_problem,
    schedule,
    sign_channel_forms,
    verify_witness,
)

MAT = os.path.join(ROOT, "materials", "725", "exchange")


def load(name):
    with open(os.path.join(MAT, name), encoding="utf-8") as fh:
        return json.load(fh)


def banner(text):
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def booklet_from(forms_path):
    return Booklet(load(forms_path))


def main() -> int:
    scope = parse_scope(load("scope.json"))
    reqs = [parse_requirements(load("requirements_G.json"))]
    pu_old = parse_catalog(load("pu_old.json"))
    pu_new = parse_catalog(load("pu_new.json"))
    he_old = parse_catalog(load("he_old.json"))
    he_new = parse_catalog(load("he_new.json"))

    banner("1. Directed channel Pu -> He, old/old: universally certified")
    print(f"  transcripts in scope: {len(enumerate_transcripts(scope))}")
    res = check_compatibility(pu_old, he_old, reqs, scope)
    print(f"  compatible = {res.compatible}")
    print("  (certified by enumerating every in-scope transcript, not by name)")

    banner("2. Pu(new) -> He(old): refuted with a Y 1-point witness")
    res = check_compatibility(pu_new, he_old, reqs, scope)
    print(f"  compatible = {res.compatible}")
    w = res.witness
    print(f"  witness transcript: {[(r['external_course'], r['points']) for r in w['records']]}")
    print(f"  sender (Pu new) allocation: {w['sender']['assignments']}")
    print(f"  receiver (He old) group maxima: {w['receiver']['group_max_ignoring_other_groups']}")
    chk = verify_witness(w, pu_new, he_old, reqs[0])
    print(f"  re-checked by original library: witness_valid={chk['witness_valid']}")

    banner("3. Same-named codes, each group reachable alone, still refuted")
    qing = parse_catalog(load("qing_catalog.json"))
    lan = parse_catalog(load("lan_catalog.json"))
    scope_ab = parse_scope(load("scope_XY.json"))
    reqs_ab = [parse_requirements(load("requirements_AB.json"))]
    res3 = check_compatibility(qing, lan, reqs_ab, scope_ab)
    print(f"  compatible = {res3.compatible}")
    w3 = res3.witness
    print(f"  witness transcript: {[(r['external_course'], r['points']) for r in w3['records']]}")
    print(f"  sender (Qing) allocation: {w3['sender']['assignments']}")
    for g in w3["receiver"]["group_max_ignoring_other_groups"]:
        print(f"    receiver {g['group_id']}: alone max={g['max_ignoring_other_groups']}, "
              f"reachable alone={g['individually_reachable']}")
    print("  -> both groups reachable alone, but one record cannot serve both.")
    print(f"  witness re-check: {verify_witness(w3, qing, lan, reqs_ab[0])['witness_valid']}")

    banner("4. Pu must switch night 1, He night 2 -> ordering deadline blocks it")
    b = booklet_from("forms_pu_he.json")
    p1 = parse_switch_problem(load("problem_pu_first.json"))
    r1 = schedule(p1, b)
    print(f"  feasible = {r1.feasible}")
    for k, v in r1.constraints.items():
        print(f"  {k}: kind={v['kind']} combo={v['combo']} forms={v['form_status']}")
    for blk in r1.diagnostics.get("blockers", []):
        print(f"    [{blk['type']}] {blk['message']}")
        if blk.get("witness"):
            print(f"       witness: {[(r['external_course'], r['points']) for r in blk['witness']['records']]}")

    banner("5. Patch He's OLD catalog via a formal revision (admit Y) -> calendar")
    # Amending a published catalog revision by overwriting it must be refused
    # (the mapping must not masquerade); only a *bumped* revision is accepted.
    from gradaudit import Store
    from gradaudit.errors import RevisionConflictError

    he_old_patched = parse_catalog({
        "format": "course-catalog/1", "revision": 2,
        "courses": [{"course_id": "HE_G1", "categories": ["G"]}],
        "equivalences": {"X": ["HE_G1"], "Y": ["HE_G1"]},
    })
    with Store(":memory:") as store:
        store.import_catalog(he_old.raw_document())  # published revision 1
        forged = he_old.raw_document()
        forged["equivalences"]["Y"] = ["HE_G1"]  # same revision, new content
        try:
            store.import_catalog(forged)
            print("  UNEXPECTED: overwriting revision 1 was accepted")
        except RevisionConflictError:
            print("  overwriting published revision 1 by adding Y -> refused "
                  "(revision_conflict); must publish a NEW revision")
        store.import_catalog(he_old_patched.raw_document())  # formal revision 2
        print(f"  published revision 2 admitting Y; "
              f"list_candidates(2,'Y') = {store.list_candidates(2, 'Y')}")
    b5 = Booklet()
    for f in sign_channel_forms(
        sender_school="pu", receiver_school="he",
        sender_old=pu_old, sender_new=pu_new,
        receiver_old=he_old_patched, receiver_new=he_new,
        requirements_list=reqs, scope=scope,
    ):
        b5.add(f)
    p5 = parse_switch_problem({
        "format": "switch-problem/1", "nights": 2, "night_capacity": 1,
        "schools": [
            {"name": "pu", "old_catalog": pu_old.raw_document(),
             "new_catalog": pu_new.raw_document(), "allowed_nights": [1]},
            {"name": "he", "old_catalog": he_old_patched.raw_document(),
             "new_catalog": he_new.raw_document(), "allowed_nights": [2]},
        ],
        "channels": [{"sender": "pu", "receiver": "he"}],
    })
    r5 = schedule(p5, b5)
    print(f"  feasible = {r5.feasible}, plan = {r5.switch_plan()}")

    banner("6. Keep original catalogs, swap nights (He 1st, Pu 2nd) -> calendar")
    p6 = parse_switch_problem(load("problem_he_first.json"))
    r6 = schedule(p6, b)
    print(f"  feasible = {r6.feasible}, plan = {r6.switch_plan()}")
    for batch in r6.calendar:
        print(f"  night {batch.night}: switch {list(batch.switched)} "
              f"-> {batch.versions_after}; channels_ok={batch.channels_ok}")

    banner("7. Reverse channel added -> Pu and He must co-switch")
    br = booklet_from("forms_reverse.json")
    pr = parse_switch_problem(load("problem_reverse.json"))
    rr = schedule(pr, br)
    print(f"  feasible = {rr.feasible}")
    print(f"  kinds: { {k: v['kind'] for k, v in rr.constraints.items()} }")
    print(f"  co-switch groups: {rr.diagnostics.get('co_switch_groups')}")
    for blk in rr.diagnostics.get("blockers", []):
        print(f"    [{blk['type']}] {blk['message']}")

    banner("8. Archive signed forms and re-read them before the switch window")
    with CompatibilityStore(":memory:") as cs:
        for f in sign_channel_forms(
            sender_school="pu", receiver_school="he",
            sender_old=pu_old, sender_new=pu_new,
            receiver_old=he_old, receiver_new=he_new,
            requirements_list=reqs, scope=scope,
        ):
            cs.save(f)
        print("  archived forms:")
        for row in cs.list_forms():
            print(f"    {row['sender_school']}->{row['receiver_school']} "
                  f"{row['sender_version']}/{row['receiver_version']}: "
                  f"compatible={bool(row['compatible'])}")
        # Re-derive constraints using the archived booklet.
        backed = Booklet(cs.find_forms())
        cons = derive_constraints(p1, backed)
        print(f"  constraints re-derived from archive: "
              f"{ {k: v.kind for k, v in cons.items()} }")

    print("\nOK: all exchange / scheduling demonstrations completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

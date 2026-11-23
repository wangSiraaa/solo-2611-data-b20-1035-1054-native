#!/usr/bin/env python3
"""Offline demonstration of the exhibition-rights / label-delivery pipeline.

Everything is driven by ``materials/rights/dataset.json``.  The script walks
through:

* rights clearance over the catalog DAG (specificity reduction, union across
  parallel nodes, intersection across holders, holds, edge deletion);
* per-composition feasibility (licence, signature placement, cumulative
  protection);
* whole-batch preference solving in application-number order;
* the central model (baked masters, receipts vs. retained samples);
* the branch model (plates + vouchers, joint-redemption guard);
* an audit that traces one shipped label through crop, signature and the
  accumulated public record.

Run with ``python3 rights_demo.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cs import dataset, delivery, exhibit, rights  # noqa: E402

DATA = ROOT / "materials" / "rights" / "dataset.json"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_clearance(name: str, clearance: rights.Clearance) -> None:
    print(f"\n-- {name}: ticket {clearance.ticket_id} status={clearance.status}")
    if not clearance.cleared:
        print(f"   HELD by holders without an applicable term: {clearance.held_holders}")
        return
    print(f"   licensed region cells: {sorted(clearance.region)}")
    print(f"   region as rects:       {clearance.region_rects}")
    print(f"   attribution strings:   {clearance.attribution}")
    print(f"   protection groups:     {[list(g) for g in clearance.protections]}")
    for h in clearance.holders:
        print(
            f"     holder {h.holder}: kept={h.kept_nodes} dropped_ancestors="
            f"{h.dropped_ancestors} region={sorted(h.region)}"
        )


def show_batch(ds: dataset.Dataset, res: exhibit.BatchResult) -> None:
    print(f"   batch status: {res.status}")
    if res.status == "delivered":
        for item in res.solved:
            comp = item.composition
            sig = item.check.signature
            edges = {p.block_index: p.edge for p in sig.placements} if sig else {}
            print(
                f"     {item.label_id} (app {item.application_no}): "
                f"composition {comp.id}"
            )
            print(
                f"        crop cells {sorted(item.check.printed)}; "
                f"signature edge {edges}; "
                f"signature cells {sorted(sig.occupied) if sig else []}"
            )
    else:
        for diag in res.diagnostics:
            print(f"     BLOCKED at {diag['blocking_label']}: {diag['explanation']}")
            for comp in diag["compositions"]:
                print(f"        {comp['composition']}: {comp['reasons']}")


def draw_scenario(ds: dataset.Dataset, sid: str) -> None:
    scen = ds.scenario(sid)
    banner(f"scenario {sid}: {scen['title']}")
    labels = ds.label_objects(scen)
    cleared = ds.license_for(scen["ticket"])
    show_clearance(scen["ticket"], cleared)
    pub = dataset.initial_public(ds, scen)
    print(f"   initial public cells: { {o: sorted(c) for o, c in pub.items()} }")
    for label in labels:
        print(f"\n   label {label.id} on {label.branch} "
              f"(app {label.application_no}, panel {label.panel}, "
              f"reserved {sorted(label.reserved)})")
        for comp in label.compositions:
            ch = dataset.check_composition(ds, label, comp, pub)
            print(f"     composition {comp.id}: feasible={ch.feasible}"
                  f"{'' if ch.feasible else ' reasons=' + str(ch.reasons)}")
            print(exhibit.render_check(
                comp, ch, panel=label.panel, reserved=label.reserved,
                originals=ds.originals,
            ).replace("\n", "\n     "))
    print()
    res = dataset.batch(ds, scen)
    show_batch(ds, res)


def demo_edge_deletion(ds: dataset.Dataset) -> None:
    banner("edge deletion: remove X->B; composition 3 loses its licence")
    edges = [e for e in ds.edges if e != ("X", "B")]
    original = ds.license_for("T-X")
    reduced = ds.license_for("T-X", edges=edges)
    show_clearance("before deletion", original)
    show_clearance("after deleting X->B", reduced)
    scen = ds.scenario("A-worked")
    labels = ds.label_objects(scen)
    labels[0].license_region = set(reduced.region)
    pub = dataset.initial_public(ds, scen)
    print("   re-checking scenario A compositions under the reduced licence:")
    for comp in labels[0].compositions:
        ch = dataset.check_composition(ds, labels[0], comp, pub)
        print(f"     {comp.id}: feasible={ch.feasible} reasons={ch.reasons}")


def central_model(ds: dataset.Dataset) -> None:
    banner("central model: baked masters, receipts vs. retained samples")
    scen = ds.scenario("A-worked")
    ds_label = ds.label_objects(scen)[0]
    record = delivery.PublicRecord()
    pub = dataset.initial_public(ds, scen)
    record.cells = {o: set(c) for o, c in pub.items()}

    master = delivery.bake_master(
        label_id=ds_label.id,
        composition=ds_label.compositions[1],
        panel=ds_label.panel,
        reserved=ds_label.reserved,
        signature_lengths=[ds_label.signature_length],
        license_region=ds_label.license_region,
        record=record,
        protections=ds.protections(),
        original_ids=ds.originals.keys(),
    )
    print(f"   baked master {master.label_id} digest={master.digest[:12]}…")
    delivery.retain_master(master, record=record, note="archive proof copy")
    print(f"   after retained sample, public cells: "
          f"{ {o: sorted(c) for o, c in record.cells.items()} }")
    delivery.dispatch_master(master, receipt="REC-001", record=record)
    print(f"   after dispatch + receipt REC-001, public cells: "
          f"{ {o: sorted(c) for o, c in record.cells.items()} }")
    print(f"   audit: {json.dumps(record.audit(master.label_id)['events'], ensure_ascii=False)}")


def branch_model(ds: dataset.Dataset) -> None:
    banner("branch model: plates + vouchers, joint redemption refused")
    scen = ds.scenario("D-joint")
    pub = dataset.initial_public(ds, scen)
    comps = {l.id: l.compositions[0] for l in ds.label_objects(scen)}

    # each branch's slice is legal when checked in isolation, from the same
    # starting public record
    for vid, label_id, branch, cells in (
        ("V-D1", "D1", "三馆", {(0, 0), (0, 1)}),
        ("V-D2", "D2", "四馆", {(0, 2)}),
    ):
        record = delivery.PublicRecord()
        record.cells = {o: set(c) for o, c in pub.items()}
        book = delivery.VoucherBook()
        book.issue(voucher_id=vid, label_id=label_id, branch=branch,
                   original="O1", cells=cells)
        delivery.redeem_batch(book, [vid], record=record,
                              protections=ds.protections(),
                              compositions=comps)
        print(f"   isolated redemption of {vid} ({branch}) is legal: "
              f"public={sorted(record.cells['O1'])}")

    # but redeeming both together would complete the forbidden group
    record2 = delivery.PublicRecord()
    record2.cells = {o: set(c) for o, c in pub.items()}
    book2 = delivery.VoucherBook()
    book2.issue(voucher_id="W1", label_id="D1", branch="三馆",
                original="O1", cells={(0, 0), (0, 1)})
    book2.issue(voucher_id="W2", label_id="D2", branch="四馆",
                original="O1", cells={(0, 2)})
    print("\n   joint redemption attempt (both vouchers at once):")
    try:
        delivery.redeem_batch(book2, ["W1", "W2"], record=record2,
                              protections=ds.protections(), compositions=comps)
        print("     unexpectedly accepted")
    except delivery.DeliveryError as exc:
        print(f"     refused: {exc}")
    print(f"     vouchers remain: {[v.status for v in book2.vouchers.values()]}")
    print(f"     public unchanged: { {o: sorted(c) for o, c in record2.cells.items()} }")

    # an issued-but-not-redeemed voucher is never treated as a definite release
    book3 = delivery.VoucherBook()
    book3.issue(voucher_id="P1", label_id="P1", branch="三馆",
                original="O1", cells={(0, 0), (0, 1)})
    book3.issue(voucher_id="P2", label_id="P2", branch="四馆",
                original="O1", cells={(0, 2)})
    book3.cancel("P2")
    record3 = delivery.PublicRecord()
    record3.cells = {o: set(c) for o, c in pub.items()}
    try:
        delivery.redeem_batch(book3, ["P2"], record=record3,
                              protections=ds.protections())
        print("   unexpectedly redeemed a cancelled voucher")
    except delivery.DeliveryError as exc:
        print(f"   cancelled voucher cannot act as a release: {exc}")


def audit_trace(ds: dataset.Dataset) -> None:
    banner("audit: trace one shipped label through crop, signature, public record")
    scen = ds.scenario("A-worked")
    label = ds.label_objects(scen)[0]
    pub = dataset.initial_public(ds, scen)
    record = delivery.PublicRecord()
    record.cells = {o: set(c) for o, c in pub.items()}
    check = dataset.check_composition(ds, label, label.compositions[1], pub)
    record.show(
        label_id=label.id,
        composition=label.compositions[1],
        check=check,
        trace={"model": "central", "receipt": "REC-001"},
    )
    entry = record.audit(label.id)
    print(json.dumps(entry, ensure_ascii=False, indent=2))


def persistent_store(ds: dataset.Dataset) -> None:
    banner("persistent delivery store: survives reopen")
    scen = ds.scenario("A-worked")
    label = ds.label_objects(scen)[0]
    pub = dataset.initial_public(ds, scen)
    path = tempfile.mktemp(suffix=".db")
    try:
        with delivery.DeliveryStore.open(path) as store:
            store.record.cells = {o: set(c) for o, c in pub.items()}
            check = dataset.check_composition(ds, label, label.compositions[1], pub)
            store.show(label_id=label.id, composition=label.compositions[1],
                       check=check, trace={"model": "central"})
            store.issue(voucher_id="V1", label_id="L1", branch="一馆",
                        original="O1", cells={(0, 3)})
        with delivery.DeliveryStore.open(path) as store:
            print(f"   reopened public cells: "
                  f"{ {o: sorted(c) for o, c in store.record.cells.items()} }")
            print(f"   reopened vouchers: {[v.as_dict() for v in store.book.vouchers.values()]}")
            print(f"   audit events: {len(store.audit(label.id)['events'])}")
    finally:
        Path(path).unlink(missing_ok=True)


def main() -> int:
    ds = dataset.load(DATA)
    banner("offline dataset")
    branches = sorted({l["branch"] for s in ds.raw["scenarios"] for l in s["labels"]})
    per_scen = {s["id"]: len(s["labels"]) for s in ds.raw["scenarios"]}
    print(f"   branches: {branches}  (cap {ds.raw['caps']['branches']})")
    print(f"   labels per scenario: {per_scen}  (cap {ds.raw['caps']['labels']})")
    print(f"   max compositions per label: "
          f"{max(len(l['compositions']) for s in ds.raw['scenarios'] for l in s['labels'])}"
          f"  (cap {ds.raw['caps']['compositions']})")

    for sid in ("A-worked", "C-emblem", "D-joint", "I-preference",
                "H-identity", "M-intersection", "U-union"):
        draw_scenario(ds, sid)

    demo_edge_deletion(ds)
    demo_hold(ds)
    central_model(ds)
    branch_model(ds)
    audit_trace(ds)
    persistent_store(ds)
    print("\nDone.")
    return 0


def demo_hold(ds: dataset.Dataset) -> None:
    banner("hold: a registered holder with no applicable term stays pending")
    show_clearance("T-HOLD (H9 has no term anywhere)", ds.license_for("T-HOLD"))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Walk through every glazing scenario from the brief on throwaway databases.

This mirrors ``showcase.py`` for the classification store.  Each numbered
section prints what the engine decides, so the behaviour is auditable without
reading the tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from glass.cutting import (  # noqa: E402
    Defect,
    Ledger,
    PaneSpec,
    Piece,
    PlanError,
    guillotine_order,
    plan_cuts,
)
from glass.store import GlazingStore  # noqa: E402


def fresh_store() -> GlazingStore:
    path = tempfile.mktemp(suffix=".db")
    gs = GlazingStore.open(path)
    gs._tmp = path  # type: ignore[attr-defined]
    gs.store.init_catalog(
        {
            "format": "classification-catalog/1",
            "revision": "G1",
            "nodes": [
                {"id": "ALL", "name": "全部"},
                {"id": "WIN", "name": "窗"},
                {"id": "LEFT", "name": "左槽"},
                {"id": "RIGHT", "name": "右槽"},
                {"id": "ODD", "name": "斜挡槽"},
            ],
            "edges": [
                ["LEFT", "WIN"],
                ["RIGHT", "WIN"],
                ["ODD", "WIN"],
                ["WIN", "ALL"],
            ],
        }
    )
    return gs


def cleanup(gs: GlazingStore) -> None:
    path = gs._tmp  # type: ignore[attr-defined]
    gs.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)


def plain_laps(slot, opening, sides, revision=1, grain=None):
    out = [
        {"slot": slot, "install_position": "pane", "item": "opening",
         "width": opening[0], "height": opening[1], "revision": revision}
    ]
    for side, (lo, hi) in sides.items():
        out.append(
            {"slot": slot, "install_position": "pane", "item": "lap",
             "side": side, "min": lo, "max": hi, "revision": revision}
        )
    if grain is not None:
        out.append(
            {"slot": slot, "install_position": "pane", "item": "grain",
             "axis": grain, "revision": revision}
        )
    return out


def main() -> int:
    print("=" * 72)
    print("0. Two equal-looking panes, one bead swap, one sheet")
    print("=" * 72)
    gs = fresh_store()
    try:
        # Both slots carry vertical-grain glass: the grain axis is on the slip.
        gs.define_slot("LEFT", "clear", True)
        gs.define_slot("RIGHT", "clear", True)
        initial = {
            "top": (1, 1), "left": (0, 0), "right": (0, 0), "bottom": (0, 0)
        }
        gs.submit_quantity(
            "qL", "TL", 1, ["LEFT"], plain_laps("LEFT", (1, 1), initial, grain="vertical")
        )
        gs.submit_quantity(
            "qR", "TR", 1, ["RIGHT"], plain_laps("RIGHT", (1, 1), initial, grain="vertical")
        )
        slip_l = gs.sign("SLIP-L", "LEFT", "pane")
        slip_r = gs.sign("SLIP-R", "RIGHT", "pane")
        print("left slot sizes:", [(s["width"], s["height"]) for s in slip_l["sizes"]],
              "grain", slip_l["grain"])
        print("right slot sizes:", [(s["width"], s["height"]) for s in slip_r["sizes"]])
        gs.register_stock(
            "B-INIT",
            [{"id": "S", "width": 2, "height": 2, "grain": "vertical", "color": "clear"}],
            1,
        )
        plan = gs.plan("P-INIT", "B-INIT", ["SLIP-L", "SLIP-R"])
        print("initial plan cuts:", plan["cuts"])
        print("  placements:", [(p["demand"], p["crop"], p["size"], p["turn"])
                                for p in plan["placements"]])

        print("\n-- swap the left slot's right bead: two slips in one commit --")
        swapped = dict(initial, right=(1, 1))
        gs.submit_quantities(
            [
                {"survey_id": "qL2", "ticket_id": "TL", "revision": 2,
                 "direct_classes": ["LEFT"],
                 "measurements": plain_laps("LEFT", (1, 1), swapped, 2, grain="vertical")},
                {"survey_id": "qR2", "ticket_id": "TR", "revision": 2,
                 "direct_classes": ["RIGHT"],
                 "measurements": plain_laps("RIGHT", (1, 1), initial, 2, grain="vertical")},
            ]
        )
        new_l = gs.sign("SLIP-L2", "LEFT", "pane")
        print("left slot after bead swap:", [(s["width"], s["height"])
                                             for s in new_l["sizes"]])
        print("original left slip is untouched:",
              gs.get_signing("SLIP-L")["sizes"] == slip_l["sizes"])
        try:
            gs.plan("P-NEW", "B-INIT", ["SLIP-L2", "SLIP-R"])
            print("unexpectedly planned")
        except PlanError as exc:
            print("one 2x2 sheet + one cut cannot now make both panes:", exc)

        print("\n-- handoff: sheet already halved horizontally --")
        gs.register_stock(
            "B-HALF",
            [
                {"id": "H1", "width": 2, "height": 1, "grain": "vertical", "color": "clear", "y": 0},
                {"id": "H2", "width": 2, "height": 1, "grain": "vertical", "color": "clear", "y": 1},
            ],
            1,
        )
        try:
            gs.plan("P-HALF", "B-HALF", ["SLIP-L"])
            print("unexpectedly planned a tall strip")
        except PlanError as exc:
            print("same area but wrong shape/grain, cannot resume:", exc)
    finally:
        cleanup(gs)

    print("\n" + "=" * 72)
    print("1. Corner baffle: reject 2x2, approve 1x2 and 2x1")
    print("=" * 72)
    gs = fresh_store()
    try:
        gs.define_slot("ODD", "clear", False)
        # Install front: left and top may lap 0 or 1; the TL baffle is 1;
        # right and bottom lap 0.  Measured on a bench turned one quarter turn
        # clockwise, so install-left/top appear as bench-top/right and the
        # install-TL corner appears as bench-TR.  rotation=1 undoes that.
        measurements = [
            {"slot": "ODD", "install_position": "pane", "item": "opening",
             "width": 1, "height": 1, "revision": 1, "rotation": 1},
            {"slot": "ODD", "install_position": "pane", "item": "lap",
             "side": "top", "min": 0, "max": 1, "revision": 1, "rotation": 1},
            {"slot": "ODD", "install_position": "pane", "item": "lap",
             "side": "right", "min": 0, "max": 1, "revision": 1, "rotation": 1},
            {"slot": "ODD", "install_position": "pane", "item": "lap",
             "side": "bottom", "min": 0, "max": 0, "revision": 1, "rotation": 1},
            {"slot": "ODD", "install_position": "pane", "item": "lap",
             "side": "left", "min": 0, "max": 0, "revision": 1, "rotation": 1},
            {"slot": "ODD", "install_position": "pane", "item": "corner",
             "corner": "TR", "value": 1, "revision": 1, "rotation": 1},
        ]
        gs.submit_quantity("qO", "TO", 1, ["ODD"], measurements)
        print("readings (install front after undoing the turn):")
        for key, value in sorted(gs.reading_set().items()):
            print("   ", key, value.payload)
        slip = gs.sign("SLIP-O", "ODD", "pane")
        print("signed sizes:", [(s["width"], s["height"]) for s in slip["sizes"]])
        for w, h in [(2, 2), (1, 2), (2, 1)]:
            result = gs.explain_size("SLIP-O", w, h)
            verdict = "accepted" if result["accepted"] else "REFUSED"
            print(f"   {w}x{h}: {verdict}"
                  + ("" if result["accepted"] else f" ({result['reason']})"))

        print("\n-- a withdrawal restores the previous slip --")
        # A worse superseding round makes the TL baffle 0, so nothing fits.
        tight = [dict(m, revision=2) for m in measurements]
        for m in tight:
            if m["item"] == "corner":
                m["value"] = 0
        gs.submit_quantity("qO2", "TO", 2, ["ODD"], tight)
        try:
            gs.sign("SLIP-O2", "ODD", "pane")
        except Exception as exc:  # noqa: BLE001
            print("tight round cannot sign:", type(exc).__name__, exc)
        restored = gs.withdraw_quantity("TO")
        print("withdrawn", restored["withdrawn_revision"],
              "exposed", restored["exposed_revision"])
        again = gs.sign("SLIP-O3", "ODD", "pane")
        print("re-signed after withdrawal matches original:",
              [(s["width"], s["height"]) for s in again["sizes"]]
              == [(s["width"], s["height"]) for s in slip["sizes"]])
    finally:
        cleanup(gs)

    print("\n" + "=" * 72)
    print("2. Defects, several legal sizes, and a resumable cut order")
    print("=" * 72)
    # Two slots each accept 1x2 or 2x1 with vertical grain; a 3x3 sheet has a
    # defect at its centre, so placements must dodge it.
    sheet = Piece(
        "S", "S", 0, 0, 3, 3, "vertical", "clear",
        (Defect(1, 1, 1, 1),),
    )
    specs = [
        PaneSpec("A", "A", "clear", "vertical", [(1, 2), (2, 1)]),
        PaneSpec("B", "B", "clear", "vertical", [(1, 2), (2, 1)]),
    ]
    plan = plan_cuts(specs, Ledger([sheet], 4))
    for placement in plan.placements:
        print(" pane", placement.demand_id, "crop", placement.crop,
              "size", placement.size, "turn", placement.turn)
    for cut in plan.cuts:
        print("  cut", cut.as_dict())
    print("cuts used", len(plan.cuts), "remaining", plan.remaining_cuts)

    print("\n-- some full layouts admit no straight-cut order (pinwheel) --")
    pinwheel = [(0, 0, 2, 1), (2, 0, 1, 2), (1, 2, 2, 1), (0, 1, 1, 2), (1, 1, 1, 1)]
    order = guillotine_order(
        [Piece("P", "P", 0, 0, 3, 3, "vertical", "clear")], pinwheel, max_depth=40
    )
    print("pinwheel order:", "none" if order is None else order)
    grid = [(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 1, 1), (1, 1, 1, 1)]
    print("2x2 grid order:", guillotine_order(
        [Piece("G", "G", 0, 0, 2, 2, "vertical", "clear")], grid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

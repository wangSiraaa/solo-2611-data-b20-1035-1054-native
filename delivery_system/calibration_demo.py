"""End-to-end reproduction of the strip window-flower calibration scenario.

This walks the worked example from the brief plus the handoff and scale checks:

1. Fold reading vouchers for the 4-cell strip folded left-onto-right and
   right-onto-left.
2. The needle mold with one left and one right needle, a stopped right needle
   and a left needle that may go one or two layers deep.
3. Calibration sheet "B has a hole" narrows the compatible settings.
4. Order B-hole/C-intact -> ``hold`` (暂缓): some settings deliver, some don't.
5. Swap only the order's translation to right-onto-left (calibration kept):
   the left needle first meets D, so *no* setting can deliver -> ``return``
   (退单).
6. Keep the left-onto-right order but drop the C constraint -> ``go``.
7. Two needles sharing one axis: stitching each needle's own favourable depth
   over-approves and would wrongly release the order.
8. Handoff (roster and conditions), re-judging after the sheets are withdrawn,
   and stopping when calibration evidence contradicts itself.
"""

from __future__ import annotations

import json
from typing import Any

from .fold_operations import OperationMark, assess_operation_mark
from .folding import FoldPlan, FoldVoucher, StripVersion
from .handoff import build_handoff, handoff_from_dict, replay_order
from .models import SourceProduct, SourceRef, SourceSegment, TranslationEntry
from .needle_mold import (
    NeedleMold,
    PunchSheet,
    WorkOrder,
    compatible_settings,
    judge_work_order,
    naive_shared_axis_verdict,
    require_consistent,
    sheets_contradictory,
)

CELLS = ("A", "B", "C", "D")


def _mold() -> NeedleMold:
    return NeedleMold(
        name="M1",
        needle_axis=((0, "left"), (1, "right")),
        candidate_depths=(("left", (1, 2)), ("right", (0,))),
    )


def _shared_axis_mold() -> NeedleMold:
    return NeedleMold(
        name="M2-shared",
        needle_axis=((0, "X"), (1, "X")),
        candidate_depths=(("X", (1, 2)),),
    )


def _voucher_view(voucher: FoldVoucher) -> dict[str, Any]:
    return {
        "needles": {
            needle: [
                {"depth": layer["depth"], "cell": layer["cell"], "side": layer["side"]}
                for layer in layers
            ]
            for needle, layers in voucher.by_needle().items()
        },
        "cells": {
            located["cell"]: {
                "needle": located["needle"],
                "depth": located["depth"],
                "side": located["side"],
            }
            for located in voucher.to_dict()["by_cell"]
        },
    }


def _fold_reading() -> dict[str, Any]:
    left = FoldPlan(CELLS, ("L",))
    right = FoldPlan(CELLS, ("R",))
    left_v = FoldVoucher(left, 1)
    right_v = FoldVoucher(right, 1)
    return {
        "left_onto_right": _voucher_view(left_v),
        "right_onto_left": _voucher_view(right_v),
    }


def _operation_marks() -> dict[str, Any]:
    product = SourceProduct(
        document_id="STRIP",
        document_revision=1,
        segments=tuple(
            SourceSegment(segment_id=c, position=i, revision=1, text=c)
            for i, c in enumerate(CELLS)
        ),
    )
    version = StripVersion(
        document_id="STRIP", revision=1, cells=CELLS, direction="L", foldable_steps=(0,)
    )
    entry = TranslationEntry(
        translation_id="BLOCK_BC",
        document_id="STRIP",
        language="EN",
        text="BC block",
        source_refs=(SourceRef("B", 1), SourceRef("C", 1)),
    )
    plan = FoldPlan(CELLS, ("L",))
    fresh = OperationMark(
        mark_id="MK1",
        document_id="STRIP",
        translation_id="BLOCK_BC",
        source_refs=(SourceRef("B", 1), SourceRef("C", 1)),
        plan=plan,
    )
    moved = OperationMark(
        mark_id="MK1",
        document_id="STRIP",
        translation_id="BLOCK_BC",
        source_refs=(SourceRef("A", 1), SourceRef("B", 1)),
        plan=plan,
    )
    entries = {"BLOCK_BC": entry}
    usable = [entry.translation_id]
    fresh_state = assess_operation_mark(
        fresh, product, entries, version, usable_translation_ids=usable
    )
    moved_state = assess_operation_mark(
        moved, product, entries, version, usable_translation_ids=usable
    )
    return {
        "fresh_mark": {"state": fresh_state.state, "issues": list(fresh_state.issues)},
        "moved_mark": {"state": moved_state.state, "issues": list(moved_state.issues)},
    }


def _shared_axis_false_approval() -> dict[str, Any]:
    mold = _shared_axis_mold()
    plan = FoldPlan(CELLS, ("L",))
    calibration = [PunchSheet("cal", plan, 1, 0, (("B", "punched"),))]
    compatible = compatible_settings(calibration, mold)
    order = WorkOrder(
        "shared-axis-order",
        plan,
        1,
        0,
        must_punch=("A", "B", "D"),
        must_intact=("C",),
    )
    real = judge_work_order(order, calibration, mold, compatible=compatible)
    naive = naive_shared_axis_verdict(order, compatible, mold)
    return {
        "compatible_settings": compatible,
        "real_verdict": real.verdict,
        "naive_independent_needle_verdict": naive,
        "false_approval": naive == "go" and real.verdict != "go",
    }


def _handoff_section(mold: NeedleMold, calibration: list[PunchSheet]) -> dict[str, Any]:
    plan_l = FoldPlan(CELLS, ("L",))
    plan_r = FoldPlan(CELLS, ("R",))
    # New order that arrives after the sheets are sealed: B punched, C intact
    # on the left layout -> hold; foreman must still pick a passing setting.
    new_order = WorkOrder(
        "post-seal", plan_l, 1, 0, must_punch=("B",), must_intact=("C",)
    )
    deliverable = WorkOrder("post-seal-deliverable", plan_l, 1, 0, must_punch=("B",))

    section: dict[str, Any] = {}
    for mode in ("roster", "conditions"):
        handoff = build_handoff(calibration, mold, mode=mode)
        wire = handoff.to_dict()
        # round-trip through JSON-like dict, proving it is self-contained
        rebuilt = handoff_from_dict(wire)
        hold_res = replay_order(rebuilt, new_order)
        go_res = replay_order(rebuilt, deliverable)
        section[mode] = {
            "handoff_mode": rebuilt.mode,
            "roster_size": len(rebuilt.compatible_settings),
            "hold_verdict": hold_res.judgment.verdict,
            "hold_pass_setting": hold_res.passing_setting,
            "hold_fail_setting": hold_res.failing_setting,
            "deliverable_verdict": go_res.judgment.verdict,
            "deliverable_pattern": go_res.reconstructed_pattern,
            "wire_has_cell_names": any(f'"{c}"' in json.dumps(wire) for c in CELLS),
        }

    # A contradictory calibration must stop work in both modes.
    contradiction = [
        PunchSheet("c1", plan_l, 1, 0, (("B", "punched"),)),
        PunchSheet("c2", plan_l, 1, 0, (("B", "intact"),)),
    ]
    stopped = []
    for mode in ("roster", "conditions"):
        try:
            build_handoff(contradiction, mold, mode=mode)
            stopped.append({"mode": mode, "stopped": False})
        except Exception as exc:  # CalibrationContradiction
            stopped.append({"mode": mode, "stopped": True, "reason": type(exc).__name__})
    section["contradiction"] = {
        "detected": sheets_contradictory(contradiction, mold),
        "build_attempts": stopped,
    }
    return section


def _scale_check() -> dict[str, Any]:
    """32 cells, five folds, eight axes; keep the setting space <= 20000."""

    cells = tuple(f"C{i}" for i in range(32))
    plan = FoldPlan(cells, ("L", "R", "L", "R", "L"))
    axes = [f"ax{i}" for i in range(8)]
    # 2 depths per axis -> 2**8 = 256 settings, well under 20000.
    needle_axis = tuple((i, axes[i % 8]) for i in range(16))
    mold = NeedleMold(
        name="SCALE",
        needle_axis=needle_axis,
        candidate_depths=tuple((a, (1, 2)) for a in axes),
    )
    state = plan.state_after(5)
    depths_ok = all(len(plan.cells) == 32 for _ in [0])
    return {
        "cells": len(cells),
        "folds": len(plan.directions),
        "axes": len(axes),
        "settings": mold.product_size(),
        "settings_within_limit": mold.product_size() <= 20000,
        "folded_positions": len(state),
        "max_layers": max(len(stack) for stack in state),
        "five_folds_ok": depths_ok and len(plan.directions) == 5,
    }


def run_calibration_demo() -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True, "steps": [], "checks": []}

    def step(name: str, payload: dict[str, Any]) -> None:
        report["steps"].append({"name": name, **payload})

    def check(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            report["ok"] = False

    fold_reading = _fold_reading()
    step("fold_reading", fold_reading)
    check(
        "L_left_needle_B_then_C",
        [x["cell"] for x in fold_reading["left_onto_right"]["needles"][0]] == ["B", "C"],
    )
    check(
        "L_right_needle_A_then_D",
        [x["cell"] for x in fold_reading["left_onto_right"]["needles"][1]] == ["A", "D"],
    )
    check(
        "R_left_needle_D_first",
        fold_reading["right_onto_left"]["needles"][0][0]["cell"] == "D",
    )

    marks = _operation_marks()
    step("operation_marks", marks)
    check("fresh_mark_usable", marks["fresh_mark"]["state"] == "usable", marks["fresh_mark"])
    check(
        "moved_mark_rejected",
        marks["moved_mark"]["state"] == "block_mismatch",
        marks["moved_mark"],
    )

    mold = _mold()
    plan_l = FoldPlan(CELLS, ("L",))
    plan_r = FoldPlan(CELLS, ("R",))
    calibration = [PunchSheet("cal", plan_l, 1, 0, (("B", "punched"),))]
    compatible = require_consistent(calibration, mold)
    step(
        "calibration",
        {
            "mold": mold.to_dict(),
            "compatible_settings": compatible,
            "sheet": "B punched",
        },
    )
    check(
        "calibration_narrows_to_two",
        len(compatible) == 2
        and {s["left"] for s in compatible} == {1, 2}
        and all(s["right"] == 0 for s in compatible),
        compatible,
    )

    # Order 1: B punched and C intact, left-onto-right -> hold (暂缓).
    order1 = WorkOrder("order1", plan_l, 1, 0, must_punch=("B",), must_intact=("C",))
    j1 = judge_work_order(order1, calibration, mold)
    step("order1_B_hole_C_intact_L", j1.to_dict())
    check("order1_hold", j1.verdict == "hold", j1.verdict)

    # Order 1 swapped only to right-onto-left -> return (退单).
    order2 = WorkOrder("order2", plan_r, 1, 0, must_punch=("B",), must_intact=("C",))
    j2 = judge_work_order(order2, calibration, mold)
    step("order2_B_hole_C_intact_R", j2.to_dict())
    check("order2_return", j2.verdict == "return", j2.verdict)
    check(
        "order2_no_setting_delivers",
        not j2.passing_settings,
    )

    # Keep left order, only relax C -> go.
    order3 = WorkOrder("order3", plan_l, 1, 0, must_punch=("B",))
    j3 = judge_work_order(order3, calibration, mold)
    step("order3_B_hole_only_L", j3.to_dict())
    check("order3_go", j3.verdict == "go", j3.verdict)

    shared = _shared_axis_false_approval()
    step("shared_axis_false_approval", shared)
    check("shared_axis_naive_would_go", shared["naive_independent_needle_verdict"] == "go")
    check("shared_axis_real_rejects", shared["real_verdict"] != "go")
    check("shared_axis_false_approval_detected", shared["false_approval"])

    handoff = _handoff_section(mold, calibration)
    step("handoff", handoff)
    for mode in ("roster", "conditions"):
        block = handoff[mode]
        check(f"{mode}_hold_verdict", block["hold_verdict"] == "hold", block)
        check(f"{mode}_deliverable_go", block["deliverable_verdict"] == "go", block)
        check(
            f"{mode}_no_client_label_leak",
            not block["wire_has_cell_names"],
            block["wire_has_cell_names"],
        )
        check(
            f"{mode}_has_pass_and_fail",
            block["hold_pass_setting"] is not None
            and block["hold_fail_setting"] is not None,
            block,
        )
    check(
        "contradiction_detected",
        handoff["contradiction"]["detected"],
    )
    check(
        "contradiction_stops_both_modes",
        all(attempt["stopped"] for attempt in handoff["contradiction"]["build_attempts"]),
    )

    scale = _scale_check()
    step("scale_check", scale)
    check("scale_cells_32", scale["cells"] == 32)
    check("scale_five_folds", scale["five_folds_ok"])
    check("scale_within_20000", scale["settings_within_limit"], scale["settings"])
    # 32 cells folded five times collapse to a single position holding all cells.
    check("scale_max_layers_32", scale["max_layers"] == 32, scale["max_layers"])

    return report

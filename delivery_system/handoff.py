"""Sealed calibration handoff for off-site foremen.

After the master verifies the sheets they are sealed and withdrawn; a remote
foreman only receives a *handoff package* and a new fold diagram.  The foreman
must be able to re-judge a new order and reconstruct its unfolded hole pattern
**without** the original proof sheets, without other households' text, and
without the raw (untested) original-cell observations.

Two handoff modes are supported:

``roster``
    The full roster of every mold setting compatible with the sealed sheets.
    The foreman picks one and punches.  Independent axes can make this roster
    large, and any recalibration invalidates it (a new roster must be issued).

``conditions``
    A compact, de-identified transfer: per-axis depth conditions plus the whole
    group of pierced-cell counts, together with the *geometry* of each
    calibration sheet (opaque cell labels, fold diagram, stop step, start
    position).  Cell names are replaced by opaque labels, no household text and
    no per-cell observations travel.  Crucially the group counts and the
    shared-axis association (needle -> axis wiring) are **not** erased, so the
    foreman can re-derive exactly which settings are admissible and find, for a
    new order, one actual setting that can deliver and one that cannot.

Both handoffs carry the mold wiring and candidate depths.  Both let the foreman
redo an order after the proof sheets are withdrawn, and both must stop work when
the calibration evidence contradicts itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Sequence

from .errors import CalibrationContradiction, HandoffError
from .folding import FoldPlan
from .needle_mold import (
    NeedleMold,
    OrderJudgment,
    PunchSheet,
    WorkOrder,
    _punched_cells,
    compatible_settings,
    judge_order_on_sheet,
    unfold_pattern,
)

HANDOFF_FORMAT = "needle-calibration-handoff/1"
ROSTER = "roster"
CONDITIONS = "conditions"


@dataclass(frozen=True)
class SheetGeometry:
    """De-identified calibration-sheet geometry (no observations, no text)."""

    sheet_id: str
    cells: tuple[str, ...]
    directions: tuple[str, ...]
    steps: int
    start: int

    def to_sheet(self) -> PunchSheet:
        return PunchSheet(
            sheet_id=self.sheet_id,
            plan=FoldPlan(self.cells, self.directions),
            steps=self.steps,
            start=self.start,
            observations=(),
        )


@dataclass(frozen=True)
class CalibrationHandoff:
    """A self-contained calibration transfer package for one mold."""

    format: str
    mode: str
    mold: NeedleMold
    # roster mode
    compatible_settings: tuple[dict[str, int], ...] = ()
    # conditions mode
    axis_conditions: tuple[tuple[str, tuple[int, ...]], ...] = ()
    count_groups: tuple[tuple[str, tuple[int, ...]], ...] = ()
    sheet_geometry: tuple[SheetGeometry, ...] = ()
    # provenance
    evidence_sha256: str = ""
    cell_labels: tuple[tuple[str, str], ...] = ()

    @property
    def label_map(self) -> dict[str, str]:
        return {real: alias for real, alias in self.cell_labels}

    @property
    def real_map(self) -> dict[str, str]:
        return {alias: real for real, alias in self.cell_labels}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "format": self.format,
            "mode": self.mode,
            "mold": self.mold.to_dict(),
            "evidence_sha256": self.evidence_sha256,
        }
        if self.mode == ROSTER:
            payload["compatible_settings"] = [dict(s) for s in self.compatible_settings]
        else:
            payload["axis_conditions"] = {
                axis: list(depths) for axis, depths in self.axis_conditions
            }
            payload["count_groups"] = {
                sheet_id: list(counts) for sheet_id, counts in self.count_groups
            }
            payload["sheet_geometry"] = [
                {
                    "sheet_id": geo.sheet_id,
                    "cells": list(geo.cells),
                    "directions": list(geo.directions),
                    "steps": geo.steps,
                    "start": geo.start,
                }
                for geo in self.sheet_geometry
            ]
        return payload


def _evidence_digest(sheets: Sequence[PunchSheet], mold: NeedleMold) -> str:
    material = {
        "mold": mold.to_dict(),
        "sheets": [
            {
                "sheet_id": sheet.sheet_id,
                "cells": list(sheet.plan.cells),
                "directions": list(sheet.plan.directions),
                "steps": sheet.steps,
                "start": sheet.start,
                "observations": [list(o) for o in sheet.observations],
                "pierce_count": sheet.pierce_count,
            }
            for sheet in sheets
        ],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _pierced_count(sheet: PunchSheet, mold: NeedleMold,
                   setting: dict[str, int]) -> int:
    return len(_punched_cells(sheet.plan, sheet.steps, sheet.start, setting, mold))


def build_handoff(sheets: Sequence[PunchSheet], mold: NeedleMold, *,
                  mode: str = ROSTER) -> CalibrationHandoff:
    """Seal a handoff package; refuse to issue it if the evidence contradicts."""

    mode = str(mode).lower()
    if mode not in (ROSTER, CONDITIONS):
        raise HandoffError(f"unknown handoff mode {mode!r}")
    compatible = compatible_settings(sheets, mold)
    if not compatible:
        raise CalibrationContradiction(
            f"cannot issue a {mode!r} handoff: the calibration sheets admit no "
            f"single setting for mold {mold.name!r}"
        )

    # Opaque, de-identified labels for every cell name across all sheets.
    seen: list[str] = []
    for sheet in sheets:
        for cell in sheet.plan.cells:
            if cell not in seen:
                seen.append(cell)
    labels = tuple((cell, f"K{index}") for index, cell in enumerate(seen))
    label = dict(labels)

    digest = _evidence_digest(sheets, mold)

    if mode == ROSTER:
        return CalibrationHandoff(
            format=HANDOFF_FORMAT,
            mode=ROSTER,
            mold=mold,
            compatible_settings=tuple(dict(s) for s in compatible),
            evidence_sha256=digest,
            cell_labels=labels,
        )

    # conditions: per-axis live depth conditions (the de-identified
    # axis-depth conditions), the group of pierced-cell counts observed per
    # sheet under the compatible settings, and de-identified sheet geometry.
    axis_conditions = tuple(
        (axis, tuple(sorted({setting[axis] for setting in compatible})))
        for axis in mold.axes
    )
    count_groups: list[tuple[str, tuple[int, ...]]] = []
    geometry: list[SheetGeometry] = []
    for sheet in sheets:
        counts = tuple(
            sorted({_pierced_count(sheet, mold, s) for s in compatible})
        )
        count_groups.append((sheet.sheet_id, counts))
        geometry.append(
            SheetGeometry(
                sheet_id=sheet.sheet_id,
                cells=tuple(label[c] for c in sheet.plan.cells),
                directions=sheet.plan.directions,
                steps=sheet.steps,
                start=sheet.start,
            )
        )
    return CalibrationHandoff(
        format=HANDOFF_FORMAT,
        mode=CONDITIONS,
        mold=mold,
        axis_conditions=axis_conditions,
        count_groups=tuple(count_groups),
        sheet_geometry=tuple(geometry),
        evidence_sha256=digest,
        cell_labels=labels,
    )


@dataclass(frozen=True)
class HandoffResult:
    """Outcome of replaying a new order from a handoff alone."""

    mode: str
    judgment: Optional[OrderJudgment]
    chosen_setting: Optional[dict[str, int]]
    passing_setting: Optional[dict[str, int]]
    failing_setting: Optional[dict[str, int]]
    reconstructed_pattern: Optional[dict[str, object]]
    stopped: bool
    stop_reason: Optional[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "chosen_setting": None if self.chosen_setting is None else dict(self.chosen_setting),
            "passing_setting": None if self.passing_setting is None else dict(self.passing_setting),
            "failing_setting": None if self.failing_setting is None else dict(self.failing_setting),
            "reconstructed_pattern": self.reconstructed_pattern,
            "judgment": None if self.judgment is None else self.judgment.to_dict(),
        }


def _settings_meeting_conditions(handoff: CalibrationHandoff) -> list[dict[str, int]]:
    """Re-derive admissible settings from conditions mode.

    A candidate setting is admissible when (a) each axis depth lies inside the
    retained per-axis depth conditions, and (b) on every de-identified
    calibration sheet it yields a pierced count inside that sheet's retained
    count group.  Because the group is checked on all axes *jointly* through the
    mold wiring, the shared-axis association is preserved rather than collapsed.
    """

    mold = handoff.mold
    allowed: dict[str, set[int]] = {
        axis: set(depths) for axis, depths in handoff.axis_conditions
    }
    groups = {sid: set(counts) for sid, counts in handoff.count_groups}
    sheets = [geo.to_sheet() for geo in handoff.sheet_geometry]

    admissible: list[dict[str, int]] = []
    for setting in mold.settings():
        if any(setting[axis] not in allowed[axis] for axis in mold.axes):
            continue
        ok = True
        for sheet in sheets:
            if _pierced_count(sheet, mold, setting) not in groups.get(
                sheet.sheet_id, set()
            ):
                ok = False
                break
        if ok:
            admissible.append(setting)
    return admissible


def replay_order(handoff: CalibrationHandoff, order: WorkOrder) -> HandoffResult:
    """Re-judge a new order using only the handoff (proof sheets withdrawn)."""

    if handoff.format != HANDOFF_FORMAT:
        raise HandoffError(f"unsupported handoff format {handoff.format!r}")

    if handoff.mode == ROSTER:
        compatible = [dict(s) for s in handoff.compatible_settings]
    else:
        compatible = _settings_meeting_conditions(handoff)

    if not compatible:
        return HandoffResult(
            mode=handoff.mode,
            judgment=None,
            chosen_setting=None,
            passing_setting=None,
            failing_setting=None,
            reconstructed_pattern=None,
            stopped=True,
            stop_reason="contradiction: handoff admits no setting for this order",
        )

    judgment = judge_order_on_sheet(
        order.requirement(), order.layout_sheet(), compatible, handoff.mold
    )

    failing_setting = (
        dict(judgment.failing_settings[0]) if judgment.failing_settings else None
    )

    if judgment.verdict == "return":
        return HandoffResult(
            mode=handoff.mode,
            judgment=judgment,
            chosen_setting=None,
            passing_setting=None,
            failing_setting=failing_setting,
            reconstructed_pattern=None,
            stopped=True,
            stop_reason="return: no admissible setting meets the order",
        )

    passing_setting = dict(judgment.passing_settings[0])
    if passing_setting is None:  # pragma: no cover - defensive
        raise AssertionError("a passing verdict must carry a passing setting")
    pattern = unfold_pattern(order.layout_sheet(), handoff.mold, passing_setting)
    reconstructed = {
        "setting": passing_setting,
        "pattern": pattern,
        "cells": [{"cell": c, "state": pattern[c]} for c in order.plan.cells],
    }

    return HandoffResult(
        mode=handoff.mode,
        judgment=judgment,
        chosen_setting=passing_setting,
        passing_setting=passing_setting,
        failing_setting=failing_setting,
        reconstructed_pattern=reconstructed,
        stopped=judgment.verdict != "go",
        stop_reason=None if judgment.verdict == "go" else "hold: mixed verdict",
    )


def handoff_from_dict(data: dict[str, object]) -> CalibrationHandoff:
    """Rebuild a handoff package from its serialised JSON form."""

    if data.get("format") != HANDOFF_FORMAT:
        raise HandoffError(f"unsupported handoff format {data.get('format')!r}")
    mold_data = data["mold"]
    mold = NeedleMold(
        name=str(mold_data["name"]),
        needle_axis=tuple(
            (int(n), str(a)) for n, a in mold_data["needle_axis"]
        ),
        candidate_depths=tuple(
            (str(a), tuple(int(d) for d in depths))
            for a, depths in dict(mold_data["candidate_depths"]).items()
        ),
    )
    mode = str(data["mode"])
    labels = tuple(
        (str(real), str(alias))
        for real, alias in dict(data.get("cell_labels", {})).items()
    )
    if mode == ROSTER:
        settings = tuple(
            {str(a): int(d) for a, d in setting.items()}
            for setting in data.get("compatible_settings", [])
        )
        return CalibrationHandoff(
            format=HANDOFF_FORMAT,
            mode=ROSTER,
            mold=mold,
            compatible_settings=settings,
            evidence_sha256=str(data.get("evidence_sha256", "")),
            cell_labels=labels,
        )
    axis_conditions = tuple(
        (str(a), tuple(int(d) for d in depths))
        for a, depths in dict(data["axis_conditions"]).items()
    )
    count_groups = tuple(
        (str(sid), tuple(int(c) for c in counts))
        for sid, counts in dict(data.get("count_groups", {})).items()
    )
    geometry = tuple(
        SheetGeometry(
            sheet_id=str(geo["sheet_id"]),
            cells=tuple(str(c) for c in geo["cells"]),
            directions=tuple(str(d) for d in geo["directions"]),
            steps=int(geo["steps"]),
            start=int(geo["start"]),
        )
        for geo in data.get("sheet_geometry", [])
    )
    return CalibrationHandoff(
        format=HANDOFF_FORMAT,
        mode=CONDITIONS,
        mold=mold,
        axis_conditions=axis_conditions,
        count_groups=count_groups,
        sheet_geometry=geometry,
        evidence_sha256=str(data.get("evidence_sha256", "")),
        cell_labels=labels,
    )

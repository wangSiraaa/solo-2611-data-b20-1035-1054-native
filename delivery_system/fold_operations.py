"""Fold-aware operation marks bound to an adopted translation block.

On a strip version the translator registers the fold direction and which crease
positions may be folded.  For an *adopted* translation block they submit an
operation mark describing the actual lift/fold sequence used: from the current
paper's left or right half, lift it over the centre line and press it onto the
other half, recounting needle positions left-to-right after every closing.

A combined translation block may span several *consecutive* creases.  A mark is
reusable only while both its source references and the translation block it was
recorded against are still current; an old mark whose source revisions or
adopted block no longer match must not be moved onto a different block.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .folding import FoldPlan, StripVersion
from .models import SourceProduct, SourceRef, TranslationEntry


@dataclass(frozen=True)
class OperationMark:
    """A recorded fold operation for one adopted translation block."""

    mark_id: str
    document_id: str
    translation_id: str
    source_refs: tuple[SourceRef, ...]
    plan: FoldPlan
    start: int = 0

    def steps(self) -> int:
        return len(self.plan.directions)


@dataclass(frozen=True)
class OperationMarkAssessment:
    state: str
    issues: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return self.state == "usable"


def assess_operation_mark(
    mark: OperationMark,
    product: SourceProduct,
    entries: dict[str, TranslationEntry],
    strip_version: StripVersion,
    *,
    usable_translation_ids: Optional[Iterable[str]] = None,
) -> OperationMarkAssessment:
    """Judge whether an old operation mark may still be applied.

    ``usable_translation_ids`` is the set of translation ids that belong to an
    actual current exact cover; when supplied, a mark on any other block is
    rejected as ``block_mismatch`` (it cannot be re-attached to a different
    adopted block).
    """

    index = product.index_by_segment_id()
    issues: list[str] = []

    if mark.document_id != product.document_id:
        return OperationMarkAssessment(
            "unknown_document", (f"mark document {mark.document_id!r} != {product.document_id!r}",)
        )

    entry = entries.get(mark.translation_id)
    if entry is None:
        return OperationMarkAssessment(
            "unknown_translation", (f"translation {mark.translation_id!r} not found",)
        )

    if usable_translation_ids is not None and mark.translation_id not in set(
        usable_translation_ids
    ):
        issues.append(
            f"translation {mark.translation_id!r} is not part of the adopted current cover"
        )

    # Source reference freshness (exactly stored revisions).
    for ref in mark.source_refs:
        seg = index.get(ref.segment_id)
        if seg is None:
            issues.append(f"unknown segment {ref.segment_id!r}")
        elif seg.revision != ref.revision:
            issues.append(
                f"{ref.segment_id}@{ref.revision} is stale (current {seg.revision})"
            )

    # The mark's refs must match the translation entry's own refs exactly; an
    # old mark cannot be moved onto a re-labelled or different block.
    entry_refs = {(r.segment_id, r.revision) for r in entry.source_refs}
    mark_refs = {(r.segment_id, r.revision) for r in mark.source_refs}
    if entry_refs != mark_refs:
        issues.append(
            "operation mark source refs do not match the translation block's refs"
        )

    # The block must be contiguous and in current source order.
    positions = [index[r.segment_id].position for r in mark.source_refs if r.segment_id in index]
    if len(positions) == len(mark.source_refs):
        contiguous = positions == list(range(positions[0], positions[0] + len(positions)))
        if not contiguous:
            issues.append("block is not a contiguous in-order interval")

    # Fold plan must belong to the strip version and only crease allowed steps.
    if mark.plan.cells != strip_version.cells:
        issues.append("fold plan cells do not match the strip version")
    else:
        if mark.steps() and mark.plan.directions[0] != strip_version.direction:
            issues.append(
                f"fold direction {mark.plan.directions[0]!r} does not match the "
                f"strip version direction {strip_version.direction!r}"
            )
        for step in range(mark.steps()):
            if not strip_version.allows_step(step):
                issues.append(f"step {step} is not a foldable position on this version")

    if issues:
        # Distinguish the primary reason for the operator.
        if any("stale" in i for i in issues) or any("unknown segment" in i for i in issues):
            state = "stale_source"
        elif any("not part of the adopted" in i for i in issues):
            state = "block_mismatch"
        elif any("do not match the translation" in i for i in issues):
            state = "block_mismatch"
        elif any("fold" in i or "step" in i or "cells" in i for i in issues):
            state = "fold_not_allowed"
        else:
            state = "invalid"
        return OperationMarkAssessment(state, tuple(issues))
    return OperationMarkAssessment("usable", ())

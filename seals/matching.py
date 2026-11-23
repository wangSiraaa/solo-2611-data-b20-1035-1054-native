"""Pure mold/imprint matching rules (no SQLite in here).

Vocabulary
----------
* A **mold** is a fixed-order string of teeth, each ``0`` / ``1``.
* An **imprint** is one or more segments. Each segment is a string of read
  cells in strip order; ``0`` / ``1`` are visible break lines and ``?`` is a
  worn cell that still occupies one position.
* Adjacent break lines are complementary: imprint ``1`` can only cover a mold
  ``0`` tooth and imprint ``0`` only a mold ``1`` tooth; ``?`` covers either.
* Segments run along one mold in one direction. A gap between two segments may
  only skip teeth covered by a shift's waste voucher, and the skipped count
  must lie inside the voucher's authorized interval; nothing may be skipped
  inside a segment.
* Splice marks are lines between two neighboring teeth. On every adjacency a
  compared readable region covers, the mold and the imprint must both carry a
  mark or both be unmarked.

All geometry is expressed in increasing mold tooth coordinates. For a reverse
reading the strip's first cell sits on the highest tooth of the covered span.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FORWARD = "forward"
REVERSE = "reverse"
HOLE_PRESENT = "present"
HOLE_MISSING = "missing"


@dataclass(frozen=True)
class Mold:
    mold_id: str
    furnace_batch: str
    pattern: str
    splice_adjacencies: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Voucher:
    voucher_id: str
    mold_id: str
    shift_id: str
    first_tooth: int
    last_tooth: int
    min_skip: int
    max_skip: int

    def authorizes(self, mold_id: str, skip: int, low: int, high: int) -> bool:
        """True if this voucher covers ``skip`` skipped teeth strictly inside
        the open mold interval ``(low, high)`` between two segments."""
        return (
            self.mold_id == mold_id
            and self.min_skip <= skip <= self.max_skip
            and self.first_tooth > low
            and self.last_tooth < high
        )


@dataclass(frozen=True)
class Imprint:
    segments: tuple[str, ...]
    splice_marks: frozenset[int]
    locating_hole: str


@dataclass(frozen=True)
class SegmentPlacement:
    segment_index: int
    mold_start: int          # inclusive, increasing mold coordinates
    mold_end: int            # inclusive


@dataclass(frozen=True)
class GapUse:
    gap_index: int
    skip: int
    voucher_id: str
    voucher: Voucher


@dataclass(frozen=True)
class Solution:
    mold_id: str
    direction: str
    placements: tuple[SegmentPlacement, ...]
    gaps: tuple[GapUse, ...]
    furnace_batch: str

    def as_dict(self) -> dict:
        return {
            "mold_id": self.mold_id,
            "furnace_batch": self.furnace_batch,
            "direction": self.direction,
            "segments": [
                {
                    "segment_index": p.segment_index,
                    "mold_start": p.mold_start,
                    "mold_end": p.mold_end,
                }
                for p in self.placements
            ],
            "gaps": [
                {
                    "gap_index": g.gap_index,
                    "skip": g.skip,
                    "voucher_id": g.voucher_id,
                    "min_skip": g.voucher.min_skip,
                    "max_skip": g.voucher.max_skip,
                }
                for g in self.gaps
            ],
        }


@dataclass
class FailureEvidence:
    mold_id: str
    furnace_batch: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mold_id": self.mold_id,
            "furnace_batch": self.furnace_batch,
            "reasons": self.reasons,
        }


# ---------------------------------------------------------------------------
# validation of an imprint read
# ---------------------------------------------------------------------------
def validate_imprint(imprint: Imprint, max_cells: int = 20) -> None:
    if not isinstance(imprint.segments, tuple) or not imprint.segments:
        raise ValueError("imprint must have at least one segment")
    total = 0
    for index, segment in enumerate(imprint.segments):
        if not segment or any(c not in "01?" for c in segment):
            raise ValueError(
                f"segment {index} must be a non-empty string of 0/1/?, got {segment!r}"
            )
        total += len(segment)
    if total > max_cells:
        raise ValueError(f"imprint has {total} cells; at most {max_cells} are allowed")
    if imprint.locating_hole not in (HOLE_PRESENT, HOLE_MISSING):
        raise ValueError(f"unknown locating_hole {imprint.locating_hole!r}")
    for mark in sorted(imprint.splice_marks):
        if mark < 0 or mark >= total - 1:
            raise ValueError(
                f"splice mark at local adjacency {mark} is outside the readable "
                f"region of {total} cells"
            )
    # marks may not sit on a segment boundary: the boundary is the adjacency
    # between two segments, i.e. just before each segment's first flat cell
    boundary = 0
    for segment in imprint.segments[:-1]:
        boundary += len(segment)
        if (boundary - 1) in imprint.splice_marks:
            raise ValueError(
                f"splice mark at local adjacency {boundary - 1} falls on a "
                "segment gap; only vouchers may govern gaps"
            )


def _directions_for(imprint: Imprint) -> tuple[str, ...]:
    # Reverse reading is allowed only when the locating hole tore off whole.
    if imprint.locating_hole == HOLE_MISSING:
        return (FORWARD, REVERSE)
    return (FORWARD,)


def _bit_compatible(cell: str, mold_bit: str) -> bool:
    if cell == "?":
        return True
    # complementary break lines: imprint 1 -> mold 0, imprint 0 -> mold 1
    return cell != mold_bit


def _flatten(imprint: Imprint) -> str:
    return "".join(imprint.segments)


def _strip_to_mold(
    direction: str, segment_offset: int, segment_mold_lo: int, segment_len: int
) -> int:
    """Map a cell offset inside one segment to its mold tooth.

    A segment occupies a contiguous ``segment_len`` interval whose low mold
    coordinate is ``segment_mold_lo``. Reverse reading walks that interval
    from the high tooth down.
    """
    if direction == FORWARD:
        return segment_mold_lo + segment_offset
    return segment_mold_lo + segment_len - 1 - segment_offset


def _adjacency_to_mold(
    direction: str, segment_offset: int, segment_mold_lo: int, segment_len: int
) -> int:
    """Mold adjacency (between tooth k and k+1) for an in-segment adjacency."""
    if direction == FORWARD:
        return segment_mold_lo + segment_offset
    # reverse adjacency k sits between the teeth of offsets k and k+1
    return segment_mold_lo + segment_len - 2 - segment_offset


# ---------------------------------------------------------------------------
# placement enumeration
# ---------------------------------------------------------------------------
def _enumerate_skip_vectors(gap_count: int, total_skip: int) -> list[list[int]]:
    """All vectors of ``gap_count`` positive integers summing to ``total_skip``."""
    if gap_count == 0:
        return [[]] if total_skip == 0 else []
    out: list[list[int]] = []

    def backtrack(remaining: int, prefix: list[int]) -> None:
        if len(prefix) == gap_count - 1:
            out.append(prefix + [remaining])
            return
        for skip in range(1, remaining - (gap_count - len(prefix) - 1) + 1):
            backtrack(remaining - skip, prefix + [skip])

    backtrack(total_skip, [])
    return out


def _candidate_spans(
    mold: Mold,
    imprint: Imprint,
    direction: str,
) -> list[tuple[list[SegmentPlacement], list[int]]]:
    """Every bit-compatible geometric placement, with per-gap skip counts.

    Vouchers are *not* checked here; the provisional gaps are resolved later so
    that a failed voucher resolution can be reported as distinct evidence.
    """
    flat = _flatten(imprint)
    n = len(flat)
    m = len(mold.pattern)
    gap_count = len(imprint.segments) - 1
    results: list[tuple[list[SegmentPlacement], list[int]]] = []

    # span length is n + total_skip and must fit the m teeth, so total_skip
    # ranges from one tooth per gap up to (m - n) inclusive
    max_total_skip = 0 if gap_count == 0 else m - n
    for total_skip in range(gap_count, max_total_skip + 1):
        span_len = n + total_skip
        for skips in _enumerate_skip_vectors(gap_count, total_skip):
            for span_start in range(0, m - span_len + 1):
                placement = _try_placement(
                    mold, imprint, direction, span_start, span_len, skips
                )
                if placement is not None:
                    results.append(placement)
    return results

def _try_placement(
    mold: Mold,
    imprint: Imprint,
    direction: str,
    span_start: int,
    span_len: int,
    skips: list[int],
) -> tuple[list[SegmentPlacement], list[int]] | None:
    placements: list[SegmentPlacement] = []
    mold_cursor = span_start  # always increasing mold coordinate
    for s_index, segment in enumerate(imprint.segments):
        seg_len = len(segment)
        lo = mold_cursor
        hi = mold_cursor + seg_len - 1
        for offset, cell in enumerate(segment):
            mold_pos = _strip_to_mold(direction, offset, lo, seg_len)
            if not _bit_compatible(cell, mold.pattern[mold_pos]):
                return None
        placements.append(SegmentPlacement(s_index, lo, hi))
        mold_cursor += seg_len
        if s_index < len(imprint.segments) - 1:
            mold_cursor += skips[len(placements) - 1]
    return placements, list(skips)


def _resolve_vouchers(
    mold: Mold,
    placements: list[SegmentPlacement],
    skips: list[int],
    vouchers: tuple[Voucher, ...],
) -> list[tuple[GapUse, ...]]:
    """Backtrack concrete voucher choices for every inter-segment gap."""
    options_per_gap: list[list[Voucher]] = []
    for gap_index, skip in enumerate(skips):
        low = placements[gap_index].mold_end
        high = placements[gap_index + 1].mold_start
        options = [
            v for v in vouchers
            if v.authorizes(mold.mold_id, skip, low, high)
        ]
        if not options:
            return []
        options_per_gap.append(options)

    resolved: list[tuple[GapUse, ...]] = []

    def backtrack(index: int, acc: list[GapUse]) -> None:
        if index == len(options_per_gap):
            resolved.append(tuple(acc))
            return
        skip = skips[index]
        for voucher in options_per_gap[index]:
            acc.append(GapUse(index, skip, voucher.voucher_id, voucher))
            backtrack(index + 1, acc)
            acc.pop()

    backtrack(0, [])
    return resolved


def _check_splice_marks(
    mold: Mold,
    imprint: Imprint,
    direction: str,
    placements: list[SegmentPlacement],
) -> list[str]:
    """Violations of the mark-agreement rule on readable adjacencies."""
    violations: list[str] = []
    flat_cursor = 0
    for s_index, segment in enumerate(imprint.segments):
        placement = placements[s_index]
        for offset in range(len(segment) - 1):
            # flat adjacency identifies the mark; the in-segment offset maps
            # it to a mold adjacency inside this segment's interval
            flat_adjacency = flat_cursor + offset
            mold_adjacency = _adjacency_to_mold(
                direction,
                offset,
                placement.mold_start,
                len(segment),
            )
            imprint_has = flat_adjacency in imprint.splice_marks
            mold_has = mold_adjacency in mold.splice_adjacencies
            if imprint_has != mold_has:
                violations.append(
                    f"splice disagreement at strip adjacency {flat_adjacency} "
                    f"(mold adjacency {mold_adjacency}): imprint "
                    f"{'has' if imprint_has else 'lacks'} a mark but mold "
                    f"{'has' if mold_has else 'lacks'} one"
                )
        flat_cursor += len(segment)
    return violations


def reconstruct(
    imprint: Imprint,
    molds: tuple[Mold, ...],
    vouchers: tuple[Voucher, ...],
) -> tuple[list[Solution], list[FailureEvidence]]:
    """Return every valid reconstruction and, for molds that cannot work,
    the concrete visible evidence that invalidates them."""
    validate_imprint(imprint)
    solutions: list[Solution] = []
    failures: list[FailureEvidence] = []
    seen: set[tuple] = set()

    for mold in molds:
        evidence = FailureEvidence(mold.mold_id, mold.furnace_batch)
        shortest = min(len(s) for s in imprint.segments)
        if len(mold.pattern) < shortest:
            evidence.reasons.append(
                f"mold has {len(mold.pattern)} teeth, fewer than the shortest "
                f"segment of {shortest} cells"
            )
        for direction in _directions_for(imprint):
            candidates = _candidate_spans(mold, imprint, direction)
            if not candidates:
                evidence.reasons.append(
                    f"no {direction} placement matches the visible 0/1 break "
                    "lines under the complement rule with legal segment gaps"
                )
                continue
            for placements, skips in candidates:
                mark_violations = _check_splice_marks(
                    mold, imprint, direction, placements
                )
                if mark_violations:
                    evidence.reasons.extend(mark_violations)
                    continue
                voucher_sets = _resolve_vouchers(mold, placements, skips, vouchers)
                if not voucher_sets:
                    intervals = [
                        (placements[i].mold_end, placements[i + 1].mold_start, skips[i])
                        for i in range(len(skips))
                    ]
                    evidence.reasons.append(
                        f"{direction} placement covering {intervals} has gaps "
                        "not covered by any shift waste voucher"
                    )
                    continue
                for gaps in voucher_sets:
                    key = (
                        mold.mold_id,
                        direction,
                        tuple((p.mold_start, p.mold_end) for p in placements),
                        tuple((g.gap_index, g.voucher_id) for g in gaps),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    solutions.append(
                        Solution(
                            mold.mold_id,
                            direction,
                            tuple(placements),
                            gaps,
                            mold.furnace_batch,
                        )
                    )
        if not any(s.mold_id == mold.mold_id for s in solutions):
            if not evidence.reasons:
                evidence.reasons.append("no reconstruction satisfied all rules")
            failures.append(evidence)
    return solutions, failures


def possible_batches(solutions: list[Solution]) -> list[str]:
    return sorted({s.furnace_batch for s in solutions})

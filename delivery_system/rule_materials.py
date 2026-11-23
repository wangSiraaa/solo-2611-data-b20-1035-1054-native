"""Concrete rule-volume materials and assembly for the gallery's rule-realization seat.

The volumes here encode the worked examples from the brief:

* ``base_four``  – 甲乙丙丁.  The opening state answers only ``试色``: 甲乙 report
  红, 丙丁 report 蓝.  After that there is exactly one mark-reading chance: the
  two red books report 方/圆 on ``转``, the two blue books report 星/月 on
  ``推``.  Wrong opening keys, or the wrong post-colour key, print 白 and blank
  the box forever; either mark-reading also locks the box.  Two steps suffice:
  colour first, then ``转`` (red) or ``推`` (blue).
* ``ding_erratum`` – 丁's real end rule changed so ``推`` reports 星, matching 丙;
  then 丙 and 丁 are permanently indistinguishable.
* ``hidden_pair`` – two volumes that print the *same* patterns for the first two
  presses but hold different internal flags, so they only separate on the third
  press.  This is the "same picture, different state, shows next round" set.
* ``broken_book`` – a book missing one promised answer, so it must be rejected
  rather than enter the candidate set.

Assembly separately enforces that a volume's clauses all belong to one published
source release: replacing an old translated block with a co-translated new block
changes that block's revision, and mixing a stale block into a rebuilt volume is
refused — the book must re-identify its clauses.  Old volumes still replay their
old rules (an old auditorium keeps replaying by the old rulebook).
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from .rules import WHITE, Clause, RulesError, Volume

# Public patterns.
RED = "红"
BLUE = "蓝"
SQUARE = "方"
CIRCLE = "圆"
STAR = "星"
MOON = "月"

# Venue keys.
KEY_COLOR = "试色"
KEY_TURN = "转"
KEY_PUSH = "推"
KEYS = (KEY_COLOR, KEY_TURN, KEY_PUSH)

DEFAULT_LAYOUT = {"color": "开局", "mark": "辨纹"}


class StaleBlockError(RulesError):
    """A translated block no longer matches the published source release."""


def _clause(
    segment_id: str,
    revision: int,
    key: str,
    *,
    on: Iterable[str] = (),
    off: Iterable[str] = (),
    rewrite: Iterable[tuple[str, bool]] = (),
    pattern: str = WHITE,
    locks: bool = False,
) -> Clause:
    return Clause(
        segment_id=segment_id,
        revision=revision,
        key=key,
        requires_on=frozenset(on),
        requires_off=frozenset(off),
        rewrite=tuple(rewrite),
        pattern=pattern,
        locks=locks,
    )


def make_book(
    volume_id: str,
    color: str,
    mark_pattern: str,
    *,
    layout: Optional[Mapping[str, str]] = None,
    revisions: Optional[Mapping[str, int]] = None,
) -> Volume:
    """A colour+mark identification book (甲/乙/丙/丁 family)."""

    layout = dict(layout or DEFAULT_LAYOUT)
    revisions = dict(revisions or {})
    color_seg = layout["color"]
    mark_seg = layout["mark"]
    rev_c = revisions.get(color_seg, 1)
    rev_m = revisions.get(mark_seg, 1)

    red = color == RED
    color_flag = "red" if red else "blue"
    mark_key = KEY_TURN if red else KEY_PUSH
    opening_off = ("red", "blue")

    clauses: list[Clause] = [
        # Opening: only 试色 answers; every other opening key blanks forever.
        _clause(color_seg, rev_c, KEY_COLOR, off=opening_off,
                rewrite=((color_flag, True),), pattern=color),
        _clause(color_seg, rev_c, KEY_TURN, off=opening_off, pattern=WHITE, locks=True),
        _clause(color_seg, rev_c, KEY_PUSH, off=opening_off, pattern=WHITE, locks=True),
        # Colour known: one mark-reading chance, then the box locks.
        _clause(mark_seg, rev_m, KEY_COLOR, on=(color_flag,), pattern=WHITE, locks=True),
    ]
    for key in (KEY_TURN, KEY_PUSH):
        pattern = mark_pattern if key == mark_key else WHITE
        clauses.append(
            _clause(mark_seg, rev_m, key, on=(color_flag,), pattern=pattern, locks=True)
        )

    return Volume(
        volume_id=volume_id,
        blocks=((color_seg, mark_seg),),
        flags=("red", "blue"),
        visible_flags=frozenset({"red", "blue"}),
        keys=KEYS,
        initial_state=frozenset(),
        clauses=tuple(clauses),
    )


def base_four() -> list[Volume]:
    return [
        make_book("甲", RED, SQUARE),
        make_book("乙", RED, CIRCLE),
        make_book("丙", BLUE, STAR),
        make_book("丁", BLUE, MOON),
    ]


def ding_erratum() -> list[Volume]:
    """The brief's erratum: 丁's real end rule now reports 星 on 推."""

    return [
        make_book("甲", RED, SQUARE),
        make_book("乙", RED, CIRCLE),
        make_book("丙", BLUE, STAR),
        make_book("丁", BLUE, STAR),  # was MOON
    ]


def make_hidden_book(volume_id: str, hidden_flag: str, mark_pattern: str) -> Volume:
    """A book whose difference from its twin lives in an internal flag.

    Both twins print 红 then 方; only the third press (推) reveals 星 vs 月.
    """

    seg1, seg2, seg3 = "开局", "辨纹", "收尾"
    clauses = (
        # opening: only colour answers
        _clause(seg1, 1, KEY_COLOR, off=("red",), rewrite=(("red", True),), pattern=RED),
        _clause(seg1, 1, KEY_TURN, off=("red",), pattern=WHITE, locks=True),
        _clause(seg1, 1, KEY_PUSH, off=("red",), pattern=WHITE, locks=True),
        # colour seen: turn prints 方 but records a *different* hidden flag
        _clause(seg2, 1, KEY_COLOR, on=("red",), off=("mark",), pattern=WHITE, locks=True),
        _clause(seg2, 1, KEY_TURN, on=("red",), off=("mark",),
                rewrite=(("mark", True), (hidden_flag, True)), pattern=SQUARE),
        _clause(seg2, 1, KEY_PUSH, on=("red",), off=("mark",), pattern=WHITE, locks=True),
        # marked: the hidden flag finally shows through
        _clause(seg3, 1, KEY_COLOR, on=("red", "mark", hidden_flag), pattern=WHITE, locks=True),
        _clause(seg3, 1, KEY_TURN, on=("red", "mark", hidden_flag), pattern=WHITE, locks=True),
        _clause(seg3, 1, KEY_PUSH, on=("red", "mark", hidden_flag),
                pattern=mark_pattern, locks=False),
    )
    return Volume(
        volume_id=volume_id,
        blocks=((seg1, seg2, seg3),),
        flags=("red", "mark", hidden_flag),
        visible_flags=frozenset({"red"}),
        keys=KEYS,
        initial_state=frozenset(),
        clauses=clauses,
    )


def hidden_pair() -> list[Volume]:
    return [
        make_hidden_book("戊", "hidden_p", STAR),
        make_hidden_book("己", "hidden_q", MOON),
    ]


def trial_trap_pair() -> list[Volume]:
    """Two red books separable on ``转`` but *permanently merged* by ``推`` first.

    壬 and 癸 both open 红.  ``转`` reports 方 vs 圆 (they are separable), but
    ``推`` before reading the mark prints 白 and locks *both* — so one wrong
    trial key drops them into the same forever-white reaction and no later step
    can recover.  A script must press ``转``, never ``推``, after 红.
    """

    return [make_book("壬", RED, SQUARE), make_book("癸", RED, CIRCLE)]


def make_stateless_book(volume_id: str, answers: Mapping[str, str]) -> Volume:
    """A toy whose every key answer is fixed (state never changes).

    Useful for separating *behaviour* (which volume) from *state machine*: the
    pattern for a key never depends on history.  No key ever locks.
    """

    seg = "固定"
    clauses = tuple(
        _clause(seg, 1, key, pattern=pattern) for key, pattern in answers.items()
    )
    return Volume(
        volume_id=volume_id,
        blocks=((seg,),),
        flags=(),
        visible_flags=frozenset(),
        keys=tuple(answers),
        initial_state=frozenset(),
        clauses=clauses,
    )


def exclusive_first_key() -> list[Volume]:
    """Three books, pairwise distinguishable, but *no single first key* splits all.

    ``x`` separates 子 from {丑,寅}; ``y`` separates 丑 from {子,寅}.  Every pair
    is distinguishable, yet neither first key alone resolves all three — the
    table must press two keys adaptively (and there is no going back to the
    opening once a key is pressed).  This is the "each pair has a discriminating
    key but the first keys are mutually exclusive" caution.
    """

    return [
        make_stateless_book("子", {"x": RED, "y": RED}),
        make_stateless_book("丑", {"x": RED, "y": BLUE}),
        make_stateless_book("寅", {"x": BLUE, "y": RED}),
    ]


def broken_book(volume_id: str = "庚") -> Volume:
    """A 甲-style book with one promised answer removed (a real gap)."""

    healthy = make_book(volume_id, RED, SQUARE)
    # Drop the post-colour 推 answer, leaving 推 unanswered in the red state.
    clauses = tuple(
        c for c in healthy.clauses
        if not (c.segment_id == "辨纹" and c.key == KEY_PUSH)
    )
    return Volume(
        volume_id=volume_id,
        blocks=healthy.blocks,
        flags=healthy.flags,
        visible_flags=healthy.visible_flags,
        keys=healthy.keys,
        initial_state=healthy.initial_state,
        clauses=clauses,
    )


# ----------------------------------------------------------------------
# release-bound assembly
# ----------------------------------------------------------------------
def check_blocks_fresh(volume: Volume, snapshot: Mapping[str, int]) -> None:
    """Every clause's segment revision must match the published release.

    ``snapshot`` maps segment_id -> published revision.  A block translated from
    an older revision is stale and must be re-translated, never relabelled.
    """

    stale = [
        (c.segment_id, c.revision, snapshot.get(c.segment_id))
        for c in volume.clauses
        if snapshot.get(c.segment_id) != c.revision
    ]
    if stale:
        ids = ", ".join(f"{sid}@{rev}!={pub}" for sid, rev, pub in sorted(set(stale)))
        raise StaleBlockError(
            f"volume {volume.volume_id!r} has stale translated block(s): {ids}; "
            "the book must re-identify its clauses against the new release"
        )


# ----------------------------------------------------------------------
# JSON interchange
# ----------------------------------------------------------------------
def clause_to_dict(clause: Clause) -> dict[str, object]:
    return {
        "segment_id": clause.segment_id,
        "revision": clause.revision,
        "key": clause.key,
        "requires_on": sorted(clause.requires_on),
        "requires_off": sorted(clause.requires_off),
        "rewrite": [[name, value] for name, value in clause.rewrite],
        "pattern": clause.pattern,
        "locks": clause.locks,
    }


def volume_to_dict(volume: Volume) -> dict[str, object]:
    return {
        "volume_id": volume.volume_id,
        "blocks": [list(block) for block in volume.blocks],
        "flags": list(volume.flags),
        "visible_flags": sorted(volume.visible_flags),
        "keys": list(volume.keys),
        "initial_state": sorted(volume.initial_state),
        "clauses": [clause_to_dict(c) for c in volume.clauses],
        "acceptance": {
            "label": volume.acceptance.label,
            "required_on": sorted(volume.acceptance.required_on),
            "required_off": sorted(volume.acceptance.required_off),
            "forbidden": sorted(volume.acceptance.forbidden),
        },
    }


def volume_from_dict(data: Mapping[str, object]) -> Volume:
    from .rules import Acceptance

    clauses = tuple(
        Clause(
            segment_id=str(c["segment_id"]),
            revision=int(c["revision"]),
            key=str(c["key"]),
            requires_on=frozenset(str(x) for x in c.get("requires_on", [])),
            requires_off=frozenset(str(x) for x in c.get("requires_off", [])),
            rewrite=tuple((str(name), bool(val)) for name, val in c.get("rewrite", [])),
            pattern=str(c.get("pattern", WHITE)),
            locks=bool(c.get("locks", False)),
        )
        for c in data["clauses"]  # type: ignore[index]
    )
    acc_raw = data.get("acceptance", {})  # type: ignore[union-attr]
    acceptance = Acceptance(
        label=str(acc_raw.get("label", "accept-all")),
        required_on=frozenset(str(x) for x in acc_raw.get("required_on", [])),
        required_off=frozenset(str(x) for x in acc_raw.get("required_off", [])),
        forbidden=frozenset(str(x) for x in acc_raw.get("forbidden", ["!locked"])),
    )
    return Volume(
        volume_id=str(data["volume_id"]),
        blocks=tuple(tuple(str(s) for s in block) for block in data["blocks"]),  # type: ignore[union-attr]
        flags=tuple(str(f) for f in data["flags"]),  # type: ignore[union-attr]
        visible_flags=frozenset(str(f) for f in data.get("visible_flags", [])),  # type: ignore[union-attr]
        keys=tuple(str(k) for k in data["keys"]),  # type: ignore[union-attr]
        initial_state=frozenset(str(f) for f in data.get("initial_state", [])),  # type: ignore[union-attr]
        clauses=clauses,
        acceptance=acceptance,
    )


def volumes_to_dict(volumes: Iterable[Volume]) -> list[dict[str, object]]:
    return [volume_to_dict(v) for v in volumes]

"""Rule-realization engine: one translated volume as a finite automaton.

A translated *volume* (one printed rulebook, assembled from real translated
blocks) is realized as a finite deterministic automaton:

* ``flags``  – internal registers the volume may hold.  A subset is *visible*
  (the standing public picture); the rest is internal state.
* ``keys``   – buttons the venue allows.  The play table only ever sees the
  public *pattern* a key press prints, never the internal flags.
* ``clauses`` – per-segment turn rules entered by the translator, each attached
  to the source revision it was translated from.  A clause names the key it
  answers, the flags that must be ON / OFF, the flags it simultaneously
  rewrites, and the pattern it prints.

Only the *first applicable clause*, in actual block order and then in-block
order, fires for one key press; the rewrite reads the pre-key state.

White-lock is hardware behaviour, not a translator rule.  A clause with
``locks=True`` drops the volume into the reserved lock flag; from then on every
key answers ``白`` forever and the box ignores all further input.  This is the
"wrong operation or wrong opening key blanks out and stays blank" trap, and the
"after identifying a mark the game is over" end state.

Because a lock auto-answers every key, it is *not* a gap.  A key with no
matching clause at a reachable, *unlocked* configuration is a genuine gap: the
translator forgot a rule.  :func:`check_completeness` reports every gap with a
walkable key history, so the missing answer is reproducible rather than
asserted.

This module is pure data + simulation.  Adaptive identification planning lives
in :mod:`delivery_system.plan`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from .errors import DeliveryError

WHITE = "白"
LOCK_FLAG = "!locked"


class RulesError(DeliveryError):
    """Malformed volume / clause / key data."""


def is_locked(state: frozenset[str]) -> bool:
    return LOCK_FLAG in state


@dataclass(frozen=True)
class Clause:
    """One play rule, bound to the source revision it was translated from.

    ``requires_on`` / ``requires_off`` are the flag *conditions*.  ``rewrite``
    is the set of flags the clause forces to a concrete value when it fires.
    ``locks`` drops the volume into the reserved lock flag (forever white).
    """

    segment_id: str
    revision: int
    key: str
    requires_on: frozenset[str] = frozenset()
    requires_off: frozenset[str] = frozenset()
    rewrite: tuple[tuple[str, bool], ...] = ()
    pattern: str = WHITE
    locks: bool = False

    def applies(self, state: frozenset[str]) -> bool:
        return self.requires_on <= state and not (self.requires_off & state)

    def next_state(self, state: frozenset[str]) -> frozenset[str]:
        if self.locks:
            return frozenset({LOCK_FLAG})
        updated = set(state)
        for name, value in self.rewrite:
            if value:
                updated.add(name)
            else:
                updated.discard(name)
        return frozenset(updated)


@dataclass(frozen=True)
class Acceptance:
    """A reachable configuration that counts as an accepted end state.

    Identification does not depend on acceptance; this is kept for venues that
    also care whether the play-through *ends* correctly, not just which volume
    it was.
    """

    label: str
    required_on: frozenset[str] = frozenset()
    required_off: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset({LOCK_FLAG})

    def accepts(self, state: frozenset[str]) -> bool:
        if is_locked(state):
            return False
        if not self.required_on <= state:
            return False
        if self.required_off & state:
            return False
        if self.forbidden & state:
            return False
        return True


@dataclass(frozen=True)
class Volume:
    """One translated rule volume, realized as a finite deterministic automaton.

    ``clauses`` are stored in *precedence order* (actual block order, then
    in-block order).  :meth:`clause_order` re-validates them and returns them
    unchanged.
    """

    volume_id: str
    blocks: tuple[tuple[str, ...], ...]  # actual block order -> segment ids
    flags: tuple[str, ...]
    visible_flags: frozenset[str]
    keys: tuple[str, ...]
    initial_state: frozenset[str]
    clauses: tuple[Clause, ...]
    acceptance: Acceptance = field(default_factory=lambda: Acceptance("accept-all"))

    def clause_order(self) -> tuple[Clause, ...]:
        indexed = {seg for block in self.blocks for seg in block}
        for clause in self.clauses:
            if clause.segment_id not in indexed:
                raise RulesError(
                    f"volume {self.volume_id!r}: clause references unknown segment "
                    f"{clause.segment_id!r}"
                )
        return self.clauses

    def revision_base(self) -> dict[str, int]:
        """Every clause in a segment must agree on that segment's revision."""

        bases: dict[str, int] = {}
        for clause in self.clauses:
            prev = bases.get(clause.segment_id)
            if prev is None:
                bases[clause.segment_id] = clause.revision
            elif prev != clause.revision:
                raise RulesError(
                    f"volume {self.volume_id!r}: segment {clause.segment_id!r} "
                    f"has clauses at conflicting revisions {prev} and {clause.revision}"
                )
        return bases

    def __post_init__(self) -> None:
        if not self.volume_id:
            raise RulesError("volume_id must be non-empty")
        flat = [seg for block in self.blocks for seg in block]
        if len(set(flat)) != len(flat):
            raise RulesError(f"volume {self.volume_id!r}: duplicate segment across blocks")
        if not self.keys:
            raise RulesError(f"volume {self.volume_id!r}: no keys declared")
        if len(set(self.keys)) != len(self.keys):
            raise RulesError(f"volume {self.volume_id!r}: duplicate keys")
        if LOCK_FLAG in self.flags:
            raise RulesError(f"volume {self.volume_id!r}: {LOCK_FLAG!r} is reserved")
        unknown_keys = {c.key for c in self.clauses} - set(self.keys)
        if unknown_keys:
            raise RulesError(
                f"volume {self.volume_id!r}: clauses use unknown keys {sorted(unknown_keys)}"
            )
        unknown_flags: set[str] = set()
        for clause in self.clauses:
            unknown_flags.update(clause.requires_on)
            unknown_flags.update(clause.requires_off)
            unknown_flags.update(name for name, _ in clause.rewrite)
        unknown_flags |= set(self.initial_state)
        unknown_flags |= set(self.acceptance.required_on)
        unknown_flags |= set(self.acceptance.required_off)
        unknown_flags -= set(self.flags)
        if unknown_flags:
            raise RulesError(
                f"volume {self.volume_id!r}: unknown flags {sorted(unknown_flags)}"
            )
        if not self.visible_flags <= set(self.flags):
            raise RulesError(
                f"volume {self.volume_id!r}: visible flags not a subset of flags"
            )
        self.revision_base()
        self.clause_order()


@dataclass(frozen=True)
class Outcome:
    """Result of one key press: public pattern + resulting internal state."""

    pattern: str
    state: frozenset[str]
    clause: Optional[Clause]
    answered: bool = True


def resolve_clause(volume: Volume, state: frozenset[str], key: str) -> Optional[Clause]:
    """The first applicable clause for ``key`` from ``state`` (or ``None``)."""

    for clause in volume.clause_order():
        if clause.key == key and clause.applies(state):
            return clause
    return None


def step(volume: Volume, state: frozenset[str], key: str) -> Outcome:
    """Advance one key press from an abstract state.

    A locked volume answers ``白`` to everything and stays locked.  An unlocked
    volume with no matching clause is *incomplete* (``answered=False``); that is
    the gap :func:`check_completeness` reports.  There is no hidden white
    fallback for unlocked states — white must be an explicit clause, so gaps are
    real.
    """

    if key not in volume.keys:
        raise RulesError(
            f"volume {volume.volume_id!r}: key {key!r} is not allowed at this table"
        )
    if is_locked(state):
        return Outcome(WHITE, state, None, answered=True)
    clause = resolve_clause(volume, state, key)
    if clause is None:
        return Outcome(WHITE, state, None, answered=False)
    return Outcome(clause.pattern, clause.next_state(state), clause, answered=True)


class Machine:
    """A live, non-resettable realization of one volume.

    There is deliberately no ``reset``: a real play test only ever has one box.
    Only complete volumes are ever driven; a missing clause here would mean an
    incomplete volume slipped past screening.
    """

    __slots__ = ("_volume", "_state")

    def __init__(self, volume: Volume) -> None:
        self._volume = volume
        self._state: frozenset[str] = volume.initial_state

    @property
    def volume_id(self) -> str:
        return self._volume.volume_id

    @property
    def state(self) -> frozenset[str]:
        return self._state

    @property
    def locked(self) -> bool:
        return is_locked(self._state)

    def observe(self) -> frozenset[str]:
        """Standing public picture (visible flags only)."""

        if self.locked:
            return frozenset()
        return frozenset(self._state & self._volume.visible_flags)

    def is_accepted(self) -> bool:
        return self._volume.acceptance.accepts(self._state)

    def press(self, key: str) -> str:
        outcome = step(self._volume, self._state, key)
        if not outcome.answered:  # pragma: no cover - guarded by screening
            raise RulesError(
                f"volume {self.volume_id!r} has no answer for {key!r} in state "
                f"{sorted(self._state)}"
            )
        self._state = outcome.state
        return outcome.pattern


def build_machine(volume: Volume) -> Machine:
    return Machine(volume)


@dataclass(frozen=True)
class WalkStep:
    key: str
    pattern: str
    state: frozenset[str]
    clause: Optional[Clause]


@dataclass(frozen=True)
class Replay:
    """A single-volume walk from the initial configuration (translator's seat)."""

    volume_id: str
    start: frozenset[str]
    steps: tuple[WalkStep, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.steps)

    @property
    def patterns(self) -> tuple[str, ...]:
        return tuple(s.pattern for s in self.steps)

    @property
    def final_state(self) -> frozenset[str]:
        return self.steps[-1].state if self.steps else self.start

    @property
    def complete(self) -> bool:
        return all(s.clause is not None or is_locked(s.state) for s in self.steps)

    def to_dict(self) -> dict[str, object]:
        return {
            "volume_id": self.volume_id,
            "start": sorted(self.start),
            "keys": list(self.keys),
            "patterns": list(self.patterns),
            "locked": is_locked(self.final_state),
            "steps": [
                {
                    "key": s.key,
                    "pattern": s.pattern,
                    "clause": None
                    if s.clause is None
                    else {
                        "segment_id": s.clause.segment_id,
                        "revision": s.clause.revision,
                        "key": s.clause.key,
                        "requires_on": sorted(s.clause.requires_on),
                        "requires_off": sorted(s.clause.requires_off),
                        "rewrite": [list(pair) for pair in s.clause.rewrite],
                        "pattern": s.clause.pattern,
                    },
                    "state": sorted(s.state),
                }
                for s in self.steps
            ],
        }


def replay(volume: Volume, keys: tuple[str, ...] | list[str]) -> Replay:
    """Walk one volume through ``keys`` from its initial configuration.

    This is the translator's seat: it yields public patterns, the clause that
    actually fired at each step, and the resulting internal state.  Only a
    single volume and a key string are needed; rejected/stale volumes are never
    passed here, so they cannot leak into a candidate set.
    """

    state = volume.initial_state
    steps: list[WalkStep] = []
    for key in keys:
        outcome = step(volume, state, key)
        state = outcome.state
        steps.append(WalkStep(key, outcome.pattern, state, outcome.clause))
    return Replay(volume.volume_id, volume.initial_state, tuple(steps))


def reachable_states(volume: Volume) -> dict[frozenset[str], tuple[str, ...]]:
    """Every reachable state with a shortest defined key history to it."""

    start = volume.initial_state
    history: dict[frozenset[str], tuple[str, ...]] = {start: ()}
    frontier: list[frozenset[str]] = [start]
    while frontier:
        state = frontier.pop(0)
        if is_locked(state):
            continue
        path = history[state]
        for key in volume.keys:
            outcome = step(volume, state, key)
            if not outcome.answered:
                continue
            if outcome.state not in history:
                history[outcome.state] = path + (key,)
                frontier.append(outcome.state)
    return history


def iter_pairs(volume: Volume) -> Iterator[tuple[frozenset[str], str, Outcome]]:
    for state in reachable_states(volume):
        for key in volume.keys:
            yield state, key, step(volume, state, key)


@dataclass(frozen=True)
class Gap:
    """An allowed key with no answer at a reachable configuration."""

    state: frozenset[str]
    key: str
    keys: tuple[str, ...]  # walkable key history from the initial configuration

    @property
    def walk(self) -> str:
        return "".join(self.keys)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": sorted(self.state),
            "key": self.key,
            "keys": list(self.keys),
            "walk": self.walk,
        }


def check_completeness(volume: Volume) -> list[Gap]:
    """Return gaps: allowed keys left unanswered at reachable unlocked states.

    The requirement is total: every venue-allowed key must answer at every
    reachable configuration.  Each gap carries a walkable key history from the
    initial configuration so a curator can reproduce the missing answer
    physically.  Locked configurations auto-answer ``白`` and are not gaps.
    """

    history = reachable_states(volume)
    gaps: list[Gap] = []
    for state, path in history.items():
        if is_locked(state):
            continue
        for key in volume.keys:
            if resolve_clause(volume, state, key) is None:
                gaps.append(Gap(state, key, path + (key,)))
    gaps.sort(key=lambda g: (sorted(g.state), g.key))
    return gaps

"""Adaptive identification planning for rule volumes.

The play table owns exactly one physical box, fixed at manufacture to one of a
candidate set of volumes.  It sees only public patterns and may press only the
keys the venue allows.  A play test cannot be reset, so we need a *single
contingent script*:

* it is deterministic (the same observation history always yields the same key);
* it selects the next key only after seeing the previous pattern;
* on stopping it must name the actual volume exactly; and
* it must never mix in a rejected (stale / gap-ridden) volume.

The search is an AND-OR walk.  Belief state is a set of ``(volume_id, state)``
pairs consistent with the patterns seen so far; pressing one key partitions the
belief by the pattern each candidate prints.  A key is usable only if *every*
branch stays solvable — observations are adversarial, the key choice is ours.

Two gallery hand-off strategies are provided and cross-checked:

* :func:`synthesize` — direct search over independent per-volume automata; each
  book is played separately and the script is assembled branch by branch.
* :func:`synthesize_merged` — first merge, *within each volume*, states with
  identical future key reactions, so common repeated configurations are built
  once.  Volume identity (and the original clause trail) is preserved, so the
  merged script can still report the right book and a single book's erratum only
  invalidates that book's quotient.

Both find the same minimal depth; the demo asserts they agree.  When no script
fits the budget we return a certificate explaining, per candidate keystroke,
which branch leaves indistinguishable candidates — never a bare "impossible".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional, Union

from .errors import DeliveryError
from .rules import (
    Machine,
    Volume,
    check_completeness,
    is_locked,
    reachable_states,
    step,
)


class PlanError(DeliveryError):
    pass


MAX_VOLUMES = 8
MAX_KEYS = 4
MAX_STEPS = 12
MAX_STATES = 32


# ----------------------------------------------------------------------
# strategy tree
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Leaf:
    """Identification finished: report this volume id."""

    volume_id: str


@dataclass(frozen=True)
class Node:
    """State-dependent key choice: ``branches[pattern]`` continues the script."""

    key: str
    branches: tuple[tuple[str, "Strategy"], ...]

    def branch(self, pattern: str) -> Optional["Strategy"]:
        for name, sub in self.branches:
            if name == pattern:
                return sub
        return None

    @property
    def patterns(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.branches)


Strategy = Union[Leaf, Node]


def strategy_depth(strategy: Strategy) -> int:
    if isinstance(strategy, Leaf):
        return 0
    return 1 + max(strategy_depth(sub) for _, sub in strategy.branches)


def strategy_to_dict(strategy: Strategy) -> dict[str, object]:
    if isinstance(strategy, Leaf):
        return {"report": strategy.volume_id}
    return {
        "key": strategy.key,
        "branches": {pattern: strategy_to_dict(sub) for pattern, sub in strategy.branches},
    }


# ----------------------------------------------------------------------
# candidate screening
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Rejection:
    volume_id: str
    reason: str
    detail: object

    def to_dict(self) -> dict[str, object]:
        return {"volume_id": self.volume_id, "reason": self.reason, "detail": self.detail}


def screen_candidates(volumes: Iterable[Volume]) -> tuple[list[Volume], list[Rejection]]:
    """Split volumes into usable candidates and rejects with reasons.

    A volume is rejected (and so never enters the candidate set) if an allowed
    key is left unanswered at a reachable unlocked configuration, or if it
    breaches the venue size limits.
    """

    accepted: list[Volume] = []
    rejected: list[Rejection] = []
    seen_ids: set[str] = set()
    for volume in volumes:
        if volume.volume_id in seen_ids:
            raise PlanError(f"duplicate volume_id {volume.volume_id!r}")
        seen_ids.add(volume.volume_id)
        if len(volume.keys) > MAX_KEYS:
            rejected.append(Rejection(volume.volume_id, "too_many_keys", len(volume.keys)))
            continue
        states = reachable_states(volume)
        if len(states) > MAX_STATES:
            rejected.append(Rejection(volume.volume_id, "too_many_states", len(states)))
            continue
        gaps = check_completeness(volume)
        if gaps:
            rejected.append(
                Rejection(
                    volume.volume_id,
                    "missing_answer",
                    {
                        "count": len(gaps),
                        "examples": [g.to_dict() for g in gaps[:8]],
                    },
                )
            )
            continue
        accepted.append(volume)
    return accepted, rejected


# ----------------------------------------------------------------------
# belief bookkeeping / state canonicalisation
# ----------------------------------------------------------------------
Belief = frozenset[tuple[str, object]]  # (volume_id, state-or-quotient-signature)


class _Universe:
    """Candidates plus the per-volume state canonicalisation for beliefs."""

    def __init__(self, volumes: list[Volume], *, merge: bool) -> None:
        if not volumes:
            raise PlanError("no candidate volumes supplied")
        key_sets = {tuple(v.keys) for v in volumes}
        if len(key_sets) != 1:
            raise PlanError(f"candidates disagree on allowed keys: {sorted(key_sets)}")
        self.volumes = {v.volume_id: v for v in volumes}
        self.keys = tuple(volumes[0].keys)
        self.merge = merge
        self._sig: dict[str, Callable[[object], object]] = {}
        self._rep: dict[str, Callable[[object], object]] = {}
        for volume in volumes:
            if merge:
                sig, rep = _quotient(volume)
            else:
                sig, rep = _identity_canon(volume)
            self._sig[volume.volume_id] = sig
            self._rep[volume.volume_id] = rep

    def signature(self, volume_id: str, state: object) -> object:
        return self._sig[volume_id](state)

    def representative(self, volume_id: str, signature: object) -> object:
        return self._rep[volume_id](signature)

    def initial_belief(self) -> Belief:
        return frozenset(
            (vid, self.signature(vid, vol.initial_state))
            for vid, vol in self.volumes.items()
        )

    def volume_ids(self, belief: Belief) -> frozenset[str]:
        return frozenset(vid for vid, _ in belief)

    def children(self, belief: Belief, key: str) -> dict[str, Belief]:
        """Partition a belief by the pattern each candidate prints for ``key``."""

        buckets: dict[str, set[tuple[str, object]]] = {}
        for vid, sig in belief:
            volume = self.volumes[vid]
            state = self.representative(vid, sig)
            outcome = step(volume, state, key)  # type: ignore[arg-type]
            child_sig = self.signature(vid, outcome.state)
            buckets.setdefault(outcome.pattern, set()).add((vid, child_sig))
        return {pattern: frozenset(members) for pattern, members in buckets.items()}


def _identity_canon(volume: Volume) -> tuple[Callable[[object], object], Callable[[object], object]]:
    return (lambda s: s), (lambda s: s)


def _quotient(
    volume: Volume,
) -> tuple[Callable[[object], object], Callable[[object], object]]:
    """Merge states with identical observable behaviour, within one volume.

    Two states are equivalent when every finite key sequence yields the same
    pattern sequence (Moore minimisation over the reachable state graph).
    Returns ``(signature, representative)``.
    """

    states = list(reachable_states(volume).keys())
    index = {state: i for i, state in enumerate(states)}
    trans: list[dict[str, int]] = []
    out: list[dict[str, str]] = []
    for state in states:
        t_row: dict[str, int] = {}
        o_row: dict[str, str] = {}
        for key in volume.keys:
            outcome = step(volume, state, key)
            t_row[key] = index[outcome.state]
            o_row[key] = outcome.pattern
        trans.append(t_row)
        out.append(o_row)

    def output_sig(i: int) -> tuple[str, ...]:
        return tuple(out[i][k] for k in volume.keys)

    block_of: list[int] = []
    sig_to_block: dict[tuple[str, ...], int] = {}
    for i in range(len(states)):
        sig = output_sig(i)
        if sig not in sig_to_block:
            sig_to_block[sig] = len(sig_to_block)
        block_of.append(sig_to_block[sig])

    while True:
        ordered = sorted(set(block_of))
        renumber = {b: n for n, b in enumerate(ordered)}
        block_of = [renumber[b] for b in block_of]
        refine: dict[tuple, int] = {}
        new_block: list[int] = []
        for i in range(len(states)):
            sig = (block_of[i], tuple(block_of[trans[i][k]] for k in volume.keys))
            if sig not in refine:
                refine[sig] = len(refine)
            new_block.append(refine[sig])
        if len(set(new_block)) == len(set(block_of)):
            block_of = new_block
            break
        block_of = new_block

    rep_by_block: dict[int, object] = {}
    for state, block in zip(states, block_of):
        rep_by_block.setdefault(block, state)
    sig_map = {state: block_of[i] for i, state in enumerate(states)}

    def signature(state: object) -> object:
        if state in sig_map:  # type: ignore[operator]
            return sig_map[state]  # type: ignore[index]
        return ("unreachable", tuple(sorted(state)))  # type: ignore[arg-type]

    def representative(sig: object) -> object:
        if isinstance(sig, int) and sig in rep_by_block:
            return rep_by_block[sig]
        raise PlanError(f"cannot recover a representative for signature {sig!r}")

    return signature, representative


# ----------------------------------------------------------------------
# search
# ----------------------------------------------------------------------
def synthesize(
    volumes: Iterable[Volume], *, max_steps: int = MAX_STEPS, merge: bool = False
) -> "PlanResult":
    candidates, rejected = screen_candidates(volumes)
    if not candidates:
        return PlanResult(
            ok=False,
            strategy=None,
            depth=None,
            max_steps=max_steps,
            candidates=[],
            rejected=rejected,
            failure={"reason": "no_candidates"},
            walks={},
        )
    if len(candidates) > MAX_VOLUMES:
        return PlanResult(
            ok=False,
            strategy=None,
            depth=None,
            max_steps=max_steps,
            candidates=[v.volume_id for v in candidates],
            rejected=rejected,
            failure={
                "reason": "too_many_candidates",
                "detail": f"a table holds at most {MAX_VOLUMES} volumes, got {len(candidates)}",
            },
            walks={},
        )
    max_steps = min(max_steps, MAX_STEPS)
    try:
        universe = _Universe(candidates, merge=merge)
    except PlanError as exc:
        return PlanResult(
            ok=False,
            strategy=None,
            depth=None,
            max_steps=max_steps,
            candidates=[v.volume_id for v in candidates],
            rejected=rejected,
            failure={"reason": "venue_key_mismatch", "detail": str(exc)},
            walks={},
        )
    root = universe.initial_belief()

    strategy: Optional[Strategy] = None
    depth: Optional[int] = None
    memo: dict[tuple[Belief, int], Optional[Strategy]] = {}

    def solve(belief: Belief, budget: int) -> Optional[Strategy]:
        ids = universe.volume_ids(belief)
        if len(ids) <= 1:
            return Leaf(next(iter(ids)))
        if budget <= 0:
            return None
        memo_key = (belief, budget)
        if memo_key in memo:
            return memo[memo_key]
        memo[memo_key] = None  # provisional; a belief only recurs with less budget
        for key in universe.keys:
            parts = universe.children(belief, key)
            subs: list[tuple[str, Strategy]] = []
            ok = True
            for pattern in sorted(parts):
                sub = solve(parts[pattern], budget - 1)
                if sub is None:
                    ok = False
                    break
                subs.append((pattern, sub))
            if ok:
                node = Node(key, tuple(subs))
                memo[memo_key] = node
                return node
        return None

    # Iterative deepening yields the minimal worst-case step count.
    for budget in range(0, max_steps + 1):
        memo.clear()
        strategy = solve(root, budget)
        if strategy is not None:
            depth = budget
            break

    if strategy is None:
        failure = _explain(universe, root, max_steps, list(universe.volumes.values()))
        return PlanResult(
            ok=False,
            strategy=None,
            depth=None,
            max_steps=max_steps,
            candidates=[v.volume_id for v in candidates],
            rejected=rejected,
            failure=failure,
            walks={},
        )

    walks = _candidate_walks(universe, strategy)
    problems = verify_plan(universe, strategy)
    return PlanResult(
        ok=True,
        strategy=strategy,
        depth=depth,
        max_steps=max_steps,
        candidates=[v.volume_id for v in candidates],
        rejected=rejected,
        failure=None,
        walks=walks,
        verification_problems=problems,
    )


def synthesize_merged(volumes: Iterable[Volume], *, max_steps: int = MAX_STEPS) -> "PlanResult":
    return synthesize(volumes, max_steps=max_steps, merge=True)


# ----------------------------------------------------------------------
# per-candidate playthrough records
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class CandidateWalk:
    volume_id: str
    keys: tuple[str, ...]
    patterns: tuple[str, ...]
    states: tuple[frozenset[str], ...]
    clauses: tuple[object, ...]
    reported: str

    def to_dict(self) -> dict[str, object]:
        return {
            "volume_id": self.volume_id,
            "keys": list(self.keys),
            "patterns": list(self.patterns),
            "reported": self.reported,
            "steps": [
                {
                    "key": key,
                    "pattern": pattern,
                    "state": sorted(state),
                    "clause": None
                    if clause is None
                    else [clause.segment_id, clause.revision, clause.key],
                }
                for key, pattern, state, clause in zip(
                    self.keys, self.patterns, self.states, self.clauses
                )
            ],
        }


def _candidate_walks(universe: _Universe, strategy: Strategy) -> dict[str, CandidateWalk]:
    walks: dict[str, CandidateWalk] = {}
    for vid, volume in universe.volumes.items():
        state = volume.initial_state
        node: Strategy = strategy
        keys: list[str] = []
        patterns: list[str] = []
        states: list[frozenset[str]] = []
        clauses: list[object] = []
        while isinstance(node, Node):
            outcome = step(volume, state, node.key)
            keys.append(node.key)
            patterns.append(outcome.pattern)
            state = outcome.state
            states.append(state)
            clauses.append(outcome.clause)
            nxt = node.branch(outcome.pattern)
            if nxt is None:  # pragma: no cover - verify_plan catches this
                raise PlanError(
                    f"volume {vid!r} printed {outcome.pattern!r}, which the script "
                    f"does not branch on at key {node.key!r}"
                )
            node = nxt
        walks[vid] = CandidateWalk(
            vid, tuple(keys), tuple(patterns), tuple(states), tuple(clauses), node.volume_id
        )
    return walks


# ----------------------------------------------------------------------
# verification (the second curator)
# ----------------------------------------------------------------------
def verify_plan(universe: _Universe, strategy: Strategy) -> list[str]:
    """Re-walk the belief along every branch: the script must be total and exact.

    Checks that the script branches on exactly the observations the candidates
    can produce, and that every leaf is reached only when a single volume
    remains and names it.  This is the "complete walk proof" that must accompany
    a per-book hand-off so no branch silently sticks.
    """

    problems: list[str] = []

    def rec(belief: Belief, node: Strategy) -> None:
        ids = universe.volume_ids(belief)
        if isinstance(node, Leaf):
            if node.volume_id not in ids:
                problems.append(f"leaf reports {node.volume_id!r} not in belief {sorted(ids)}")
            if len(ids) != 1:
                problems.append(f"leaf at belief {sorted(ids)} is not a single volume")
            return
        parts = universe.children(belief, node.key)
        if set(parts) != set(node.patterns):
            problems.append(
                f"key {node.key!r}: script branches on {sorted(node.patterns)} but "
                f"observations are {sorted(parts)}"
            )
            return
        for pattern, sub in node.branches:
            rec(parts[pattern], sub)

    rec(universe.initial_belief(), strategy)
    return problems


# ----------------------------------------------------------------------
# failure certificate
# ----------------------------------------------------------------------
def _explain(
    universe: _Universe, root: Belief, budget: int, candidates: list[Volume]
) -> dict[str, object]:
    """Explain why no script fits: per first key, the branch left unresolved.

    Deliberately shallow and memoised: a blocked branch is reported with its
    remaining candidates and a short human reason, not an exponential recursive
    re-explanation.  Indistinguishable volumes are named once at the top.
    """

    equivalent = _equivalent_groups(candidates)
    explanation: dict[str, object] = {"budget": budget}
    if equivalent:
        explanation["indistinguishable_groups"] = [sorted(g) for g in equivalent]

    memos: dict[tuple[Belief, int], bool] = {}
    per_key: list[dict[str, object]] = []
    for key in universe.keys:
        parts = universe.children(root, key)
        blocked = []
        for pattern in sorted(parts):
            child = parts[pattern]
            ids = universe.volume_ids(child)
            if len(ids) <= 1:
                continue
            if _solvable(universe, child, budget - 1, memos):
                continue
            blocked.append(
                {
                    "pattern": pattern,
                    "remaining": sorted(ids),
                    "reason": _branch_reason(universe, child, budget - 1),
                    "steps_left": max(budget - 1, 0),
                }
            )
        per_key.append(
            {
                "key": key,
                "observations": {
                    pattern: sorted(universe.volume_ids(child))
                    for pattern, child in sorted(parts.items())
                },
                "blocked_branches": blocked,
            }
        )

    if equivalent:
        reason = "indistinguishable_volumes"
        detail = (
            "some volumes are behaviourally identical from their initial "
            "configurations: they print the same pattern for every key sequence, "
            "so no playthrough can tell them apart"
        )
    else:
        reason = "budget_exhausted"
        detail = (
            f"within {budget} step(s), every first key leaves at least one observed "
            "pattern whose remaining candidates cannot be separated"
        )
    explanation["reason"] = reason
    explanation["detail"] = detail
    explanation["per_key"] = per_key
    return explanation


def _branch_reason(universe: _Universe, belief: Belief, budget: int) -> str:
    if budget <= 0:
        return "no steps left; these candidates are still merged"
    if not universe.keys:
        return "no keys available"
    # A branch is stuck if for every key some observation keeps multiple books.
    for key in universe.keys:
        parts = universe.children(belief, key)
        if all(len(universe.volume_ids(child)) <= 1 for child in parts.values()):
            return f"separable by {key!r} given more budget"
    return "every remaining key leaves at least two books sharing one observation"


def _solvable(
    universe: _Universe,
    belief: Belief,
    budget: int,
    memo: Optional[dict[tuple[Belief, int], bool]] = None,
) -> bool:
    if memo is None:
        memo = {}

    def solve(b: Belief, left: int) -> bool:
        if len(universe.volume_ids(b)) <= 1:
            return True
        if left <= 0:
            return False
        key = (b, left)
        if key in memo:
            return memo[key]
        memo[key] = False
        for k in universe.keys:
            parts = universe.children(b, k)
            if all(solve(child, left - 1) for child in parts.values()):
                memo[key] = True
                return True
        return False

    return solve(belief, budget)


def _equivalent_groups(volumes: list[Volume]) -> list[list[str]]:
    groups: list[list[str]] = []
    used: set[str] = set()
    for i, a in enumerate(volumes):
        if a.volume_id in used:
            continue
        group = [a.volume_id]
        for b in volumes[i + 1 :]:
            if b.volume_id in used:
                continue
            if _behaviorally_equal(a, b):
                group.append(b.volume_id)
                used.add(b.volume_id)
        if len(group) > 1:
            used.add(a.volume_id)
            groups.append(group)
    return groups


def _behaviorally_equal(a: Volume, b: Volume) -> bool:
    keys = tuple(k for k in a.keys if k in set(b.keys))
    start = (a.initial_state, b.initial_state)
    seen = {start}
    frontier = [start]
    while frontier:
        sa, sb = frontier.pop()
        for key in keys:
            oa = step(a, sa, key)
            ob = step(b, sb, key)
            if oa.pattern != ob.pattern:
                return False
            pair = (oa.state, ob.state)
            if pair not in seen:
                seen.add(pair)
                frontier.append(pair)
    return True


# ----------------------------------------------------------------------
# result
# ----------------------------------------------------------------------
@dataclass
class PlanResult:
    ok: bool
    strategy: Optional[Strategy]
    depth: Optional[int]
    max_steps: int
    candidates: list[str]
    rejected: list[Rejection]
    failure: Optional[dict[str, object]]
    walks: Mapping[str, CandidateWalk]
    verification_problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "candidates": self.candidates,
            "rejected": [r.to_dict() for r in self.rejected],
            "max_steps": self.max_steps,
            "depth": self.depth,
            "strategy": None if self.strategy is None else strategy_to_dict(self.strategy),
            "failure": self.failure,
            "walks": {vid: walk.to_dict() for vid, walk in self.walks.items()},
            "verification_problems": list(self.verification_problems),
        }


# ----------------------------------------------------------------------
# live execution (the play table, using patterns only)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class PlayTrace:
    keys: tuple[str, ...]
    patterns: tuple[str, ...]
    reported: Optional[str]
    stuck: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "keys": list(self.keys),
            "patterns": list(self.patterns),
            "reported": self.reported,
            "stuck": self.stuck,
        }


def run_script(strategy: Strategy, machine: Machine) -> PlayTrace:
    """Operate one live box through the script, choosing keys from patterns only."""

    node: Strategy = strategy
    keys: list[str] = []
    patterns: list[str] = []
    while isinstance(node, Node):
        pattern = machine.press(node.key)
        keys.append(node.key)
        patterns.append(pattern)
        nxt = node.branch(pattern)
        if nxt is None:
            return PlayTrace(
                tuple(keys), tuple(patterns), None,
                f"key {node.key!r} printed {pattern!r}, which the script never saw",
            )
        node = nxt
    return PlayTrace(tuple(keys), tuple(patterns), node.volume_id)


def build_handoff(result: PlanResult) -> dict[str, object]:
    """A curatable hand-off: the script *plus* a complete per-candidate walk proof.

    The play table may be staffed by someone who only follows patterns; the
    proof lets a second curator confirm, without pressing anything, that every
    candidate branch is covered and each leaf names exactly one book.  If the
    plan failed, the hand-off carries the impossibility certificate instead.
    """

    if not result.ok:
        return {
            "deliverable": False,
            "candidates": result.candidates,
            "rejected": [r.to_dict() for r in result.rejected],
            "impossibility": result.failure,
        }
    proof = {
        vid: {
            "keys": list(walk.keys),
            "patterns": list(walk.patterns),
            "reports": walk.reported,
            "ends_locked": bool(walk.states) and is_locked(walk.states[-1]),
            "ends_at": sorted(walk.states[-1]) if walk.states else [],
        }
        for vid, walk in result.walks.items()
    }
    return {
        "deliverable": True,
        "candidates": result.candidates,
        "rejected": [r.to_dict() for r in result.rejected],
        "strategy": strategy_to_dict(result.strategy) if result.strategy else None,
        "depth": result.depth,
        "max_steps": result.max_steps,
        "verification_problems": list(result.verification_problems),
        "walk_proof": proof,
    }

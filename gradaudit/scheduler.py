"""Switch calendar over directed exchange channels.

Every school starts on its **old** catalog and must end on its **new** catalog,
switching *exactly once*, with no order inside a night.  Channels are directed
``sender -> receiver``; after every night (i.e. at each following morning) *all*
channels must keep accepting items at the versions both ends currently run.
A channel may never be paused to cover the transition -- the calendar is only
valid if every morning is compatible.

The lab (:mod:`gradaudit.exchange`) signs one compatibility form per directed
channel and version pair ``(old|new, old|new)``.  This module consumes those
signed forms: for each directed channel it translates the four version combos
into a *precedence*, *same-night* or *forbidden* condition::

    (o,o) / (n,n) false            -> channel broken at start / end (no calendar)
    (o,n) false, (n,o) true        -> sender must switch no later than receiver
    (n,o) false, (o,n) true        -> receiver must switch no later than sender
    (o,n) false and (n,o) false    -> both must switch the SAME night (co-switch)

It then runs an exact day-by-day DP over the set of already-switched schools,
mixing those constraints with each school's available nights and each night's
switch capacity.  If no calendar exists it reports which deadlines, forced
co-switch pair, or broken channel witness blocks the plan.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from .errors import SchemaError
from .model import Catalog, parse_catalog

MAX_SCHOOLS = 8
MAX_NIGHTS = 6

PROBLEM_FORMAT = "switch-problem/1"

OLD = "old"
NEW = "new"


# --------------------------------------------------------------------------- #
# problem description
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class School:
    name: str
    old_catalog: Catalog
    new_catalog: Catalog
    allowed_nights: Optional[FrozenSet[int]]  # None => every night

    def catalog_for(self, version: str) -> Catalog:
        if version == OLD:
            return self.old_catalog
        if version == NEW:
            return self.new_catalog
        raise SchemaError(f"version must be 'old' or 'new' (got {version!r})", location="version")

    def can_switch_on(self, night: int) -> bool:
        return self.allowed_nights is None or night in self.allowed_nights


@dataclass(frozen=True)
class Channel:
    sender: str
    receiver: str


@dataclass
class SwitchProblem:
    schools: Dict[str, School]
    channels: Tuple[Channel, ...]
    nights: int
    night_capacity: Dict[int, int]  # night -> max schools; missing => len(schools)

    def capacity(self, night: int) -> int:
        return self.night_capacity.get(night, len(self.schools))


def parse_switch_problem(doc: object) -> SwitchProblem:
    if not isinstance(doc, dict):
        raise SchemaError("switch problem must be a JSON object")
    if doc.get("format") != PROBLEM_FORMAT:
        raise SchemaError(f"format must be {PROBLEM_FORMAT!r}", location="format")

    nights = doc.get("nights")
    if not isinstance(nights, int) or isinstance(nights, bool) or nights < 1:
        raise SchemaError("nights must be a positive integer", location="nights")
    if nights > MAX_NIGHTS:
        raise SchemaError(f"at most {MAX_NIGHTS} nights are supported", location="nights")

    raw_schools = doc.get("schools")
    if not isinstance(raw_schools, list) or not raw_schools:
        raise SchemaError("schools must be a non-empty list", location="schools")
    if len(raw_schools) > MAX_SCHOOLS:
        raise SchemaError(f"at most {MAX_SCHOOLS} schools are supported", location="schools")

    schools: Dict[str, School] = {}
    for idx, entry in enumerate(raw_schools):
        loc = f"schools[{idx}]"
        if not isinstance(entry, dict):
            raise SchemaError("school must be an object", location=loc)
        name = entry.get("name")
        if not isinstance(name, str) or name.strip() == "":
            raise SchemaError("school name must be a non-empty string", location=loc)
        if name in schools:
            raise SchemaError(f"duplicate school name {name!r}", location=name)
        for key in ("old_catalog", "new_catalog"):
            if key not in entry:
                raise SchemaError(f"school {name!r} is missing {key}", location=name)
        old_cat = parse_catalog(entry["old_catalog"])
        new_cat = parse_catalog(entry["new_catalog"])
        allowed_raw = entry.get("allowed_nights")
        if allowed_raw is None:
            allowed: Optional[FrozenSet[int]] = None
        else:
            if not isinstance(allowed_raw, list) or not allowed_raw:
                raise SchemaError(
                    f"allowed_nights for {name!r} must be a non-empty list or omitted",
                    location=name,
                )
            norm = set()
            for n in allowed_raw:
                if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= nights):
                    raise SchemaError(
                        f"allowed_nights for {name!r} must be integers in 1..{nights}",
                        location=name,
                    )
                norm.add(n)
            allowed = frozenset(norm)
        schools[name] = School(name=name, old_catalog=old_cat, new_catalog=new_cat,
                               allowed_nights=allowed)

    raw_channels = doc.get("channels")
    if not isinstance(raw_channels, list):
        raise SchemaError("channels must be a list", location="channels")
    channels: List[Channel] = []
    seen_edges = set()
    for idx, entry in enumerate(raw_channels):
        if isinstance(entry, dict):
            src, dst = entry.get("sender"), entry.get("receiver")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            src, dst = entry
        else:
            raise SchemaError("channel must be {sender,receiver} or [sender,receiver]",
                              location=f"channels[{idx}]")
        if src not in schools or dst not in schools:
            raise SchemaError(
                f"channel endpoints must be known schools (got {src!r} -> {dst!r})",
                location=f"channels[{idx}]",
            )
        if (src, dst) in seen_edges:
            raise SchemaError(f"duplicate channel {src!r}->{dst!r}", location=f"channels[{idx}]")
        seen_edges.add((src, dst))
        channels.append(Channel(sender=src, receiver=dst))

    raw_cap = doc.get("night_capacity")
    night_capacity: Dict[int, int] = {}
    if raw_cap is not None:
        if isinstance(raw_cap, int) and not isinstance(raw_cap, bool):
            for n in range(1, nights + 1):
                night_capacity[n] = raw_cap
        elif isinstance(raw_cap, dict):
            for key, val in raw_cap.items():
                try:
                    n = int(key)
                except (TypeError, ValueError):
                    raise SchemaError("night_capacity keys must be night numbers", location="night_capacity")
                if not (1 <= n <= nights):
                    raise SchemaError("night_capacity night out of range", location="night_capacity")
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    raise SchemaError("night_capacity values must be non-negative integers",
                                      location="night_capacity")
                night_capacity[n] = val
        else:
            raise SchemaError("night_capacity must be an int or a night->int object",
                              location="night_capacity")

    return SwitchProblem(schools=schools, channels=tuple(channels), nights=nights,
                         night_capacity=night_capacity)


# --------------------------------------------------------------------------- #
# signed-form booklet
# --------------------------------------------------------------------------- #
@dataclass
class FormLookup:
    status: str  # ok | missing | stale | incompatible
    compatible: bool
    detail: Optional[dict] = None


class Booklet:
    """Signed compatibility forms keyed by channel + version pair.

    Only forms whose catalog hashes match the catalogs the problem actually
    uses are trusted; a mismatched hash is reported as ``stale`` rather than
    silently used (a signature for a different catalog must not certify the
    current one).
    """

    def __init__(self, forms: Optional[Sequence[object]] = None):
        # key -> form
        self._forms: Dict[Tuple[str, str, str, str], object] = {}
        if forms:
            for f in forms:
                self.add(f)

    def add(self, form) -> None:
        if isinstance(form, dict):
            from .exchange import _form_from_document
            form = _form_from_document(form)
        key = (form.sender_school, form.receiver_school, form.sender_version, form.receiver_version)
        self._forms[key] = form

    def __len__(self) -> int:
        return len(self._forms)

    def lookup(
        self,
        sender: School,
        sender_version: str,
        receiver: School,
        receiver_version: str,
        problem_catalogs: Dict[Tuple[str, str], Catalog],
    ) -> FormLookup:
        key = (sender.name, receiver.name, sender_version, receiver_version)
        form = self._forms.get(key)
        if form is None:
            return FormLookup(
                status="missing",
                compatible=False,
                detail={"channel": [sender.name, receiver.name],
                        "versions": [sender_version, receiver_version]},
            )
        sender_hash = problem_catalogs[(sender.name, sender_version)].content_hash()
        receiver_hash = problem_catalogs[(receiver.name, receiver_version)].content_hash()
        if form.sender_catalog_hash != sender_hash or form.receiver_catalog_hash != receiver_hash:
            return FormLookup(
                status="stale",
                compatible=False,
                detail={
                    "channel": [sender.name, receiver.name],
                    "versions": [sender_version, receiver_version],
                    "signed_hashes": [form.sender_catalog_hash, form.receiver_catalog_hash],
                    "current_hashes": [sender_hash, receiver_hash],
                },
            )
        return FormLookup(
            status="ok" if form.compatible else "incompatible",
            compatible=bool(form.compatible),
            detail=form.result.get("witness") if not form.compatible else None,
        )


# --------------------------------------------------------------------------- #
# derived constraints
# --------------------------------------------------------------------------- #
@dataclass
class EdgeConstraint:
    sender: str
    receiver: str
    oo: bool
    on: bool
    no: bool
    nn: bool
    statuses: Dict[str, str]  # version combo -> form status
    witnesses: Dict[str, Optional[dict]] = field(default_factory=dict)

    def status_for(self, sender_version: str, receiver_version: str) -> str:
        return self.statuses.get(f"{sender_version},{receiver_version}", "missing")

    def evidence_gaps(self) -> List[dict]:
        """Version combos that are *not usable* only because no valid signed
        form exists (``missing``/``stale``), as opposed to being proven broken."""
        gaps = []
        for combo, status in self.statuses.items():
            if status in ("missing", "stale"):
                gaps.append({"channel": [self.sender, self.receiver],
                             "combo": combo, "status": status})
        return gaps

    @property
    def kind(self) -> str:
        if not self.oo:
            return "start_forbidden"     # old,old broken
        if not self.nn:
            return "end_forbidden"       # new,new broken
        if not self.on and not self.no:
            return "same_night"
        if not self.no:                  # (n,o) broken -> receiver no later than sender
            return "order_receiver_first"
        if not self.on:                  # (o,n) broken -> sender no later than receiver
            return "order_sender_first"
        return "free"

    def describe(self) -> dict:
        return {
            "channel": [self.sender, self.receiver],
            "combo": {"old,old": self.oo, "old,new": self.on,
                      "new,old": self.no, "new,new": self.nn},
            "form_status": dict(self.statuses),
            "kind": self.kind,
        }


def derive_constraints(
    problem: SwitchProblem, booklet: Booklet
) -> Dict[Tuple[str, str], EdgeConstraint]:
    problem_catalogs = {
        (s.name, OLD): s.old_catalog for s in problem.schools.values()
    }
    problem_catalogs.update({
        (s.name, NEW): s.new_catalog for s in problem.schools.values()
    })
    out: Dict[Tuple[str, str], EdgeConstraint] = {}
    for ch in problem.channels:
        s = problem.schools[ch.sender]
        r = problem.schools[ch.receiver]
        statuses: Dict[str, str] = {}
        vals: Dict[str, bool] = {}
        witnesses: Dict[str, Optional[dict]] = {}
        for sv, rv in ((OLD, OLD), (OLD, NEW), (NEW, OLD), (NEW, NEW)):
            lk = booklet.lookup(s, sv, r, rv, problem_catalogs)
            vals[(sv, rv)] = lk.compatible
            statuses[f"{sv},{rv}"] = lk.status
            witnesses[f"{sv},{rv}"] = lk.detail if not lk.compatible else None
        out[(ch.sender, ch.receiver)] = EdgeConstraint(
            sender=ch.sender,
            receiver=ch.receiver,
            oo=vals[(OLD, OLD)],
            on=vals[(OLD, NEW)],
            no=vals[(NEW, OLD)],
            nn=vals[(NEW, NEW)],
            statuses=statuses,
            witnesses=witnesses,
        )
    return out


# --------------------------------------------------------------------------- #
# calendar result
# --------------------------------------------------------------------------- #
@dataclass
class NightBatch:
    night: int
    switched: Tuple[str, ...]
    versions_after: Dict[str, str]
    channels_ok: bool
    channel_checks: List[dict]

    def as_dict(self) -> dict:
        return {
            "night": self.night,
            "switch": list(self.switched),
            "versions_after": dict(self.versions_after),
            "channels_ok": self.channels_ok,
            "channel_checks": self.channel_checks,
        }


@dataclass
class CalendarResult:
    feasible: bool
    calendar: List[NightBatch] = field(default_factory=list)
    constraints: Dict[str, dict] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "calendar": [b.as_dict() for b in self.calendar],
            "constraints": self.constraints,
            "diagnostics": self.diagnostics,
        }

    def switch_plan(self) -> Dict[str, int]:
        """school -> night it switches on (for a feasible calendar)."""
        plan: Dict[str, int] = {}
        for batch in self.calendar:
            for name in batch.switched:
                plan[name] = batch.night
        return plan


# --------------------------------------------------------------------------- #
# exact day-by-day DP
# --------------------------------------------------------------------------- #
def _versions(state: FrozenSet[str], schools: Sequence[str]) -> Dict[str, str]:
    return {name: (NEW if name in state else OLD) for name in schools}


def _check_morning(
    problem: SwitchProblem, constraints: Dict[Tuple[str, str], EdgeConstraint], state: FrozenSet[str]
) -> Tuple[bool, List[dict]]:
    checks: List[dict] = []
    ok = True
    for ch in problem.channels:
        sv = NEW if ch.sender in state else OLD
        rv = NEW if ch.receiver in state else OLD
        ec = constraints[(ch.sender, ch.receiver)]
        good = ec.oo if (sv == OLD and rv == OLD) else \
            ec.on if (sv == OLD and rv == NEW) else \
            ec.no if (sv == NEW and rv == OLD) else ec.nn
        checks.append({
            "channel": [ch.sender, ch.receiver],
            "versions": [sv, rv],
            "compatible": good,
        })
        if not good:
            ok = False
    return ok, checks


def schedule(problem: SwitchProblem, booklet: Booklet) -> CalendarResult:
    constraints = derive_constraints(problem, booklet)
    names = sorted(problem.schools)
    all_schools = frozenset(names)

    # morning 0 (all old) must already be exchangeable
    start_ok, start_checks = _check_morning(problem, constraints, frozenset())
    # final morning (all new) must be exchangeable
    end_ok, end_checks = _check_morning(problem, constraints, all_schools)

    constraint_report = {
        f"{ec.sender}->{ec.receiver}": ec.describe() for ec in constraints.values()
    }

    # A broken start or end morning is fatal regardless of any intermediate
    # path (the network is down before the first switch or after the last).
    if not start_ok or not end_ok:
        diagnostics = _diagnose(
            problem, constraints, start_ok, end_ok, start_checks, end_checks, names
        )
        _attach_evidence_gaps(diagnostics, constraints)
        return CalendarResult(
            feasible=False,
            calendar=[],
            constraints=constraint_report,
            diagnostics=diagnostics,
        )
    # DP: states → backpointer chain.  State = frozenset of already-NEW schools.
    # frontier maps state -> (previous_state, switched_this_night)
    frontier: Dict[FrozenSet[str], Tuple[Optional[FrozenSet[str]], Tuple[str, ...]]] = {
        frozenset(): (None, tuple())
    }
    layers: List[Dict[FrozenSet[str], Tuple[Optional[FrozenSet[str]], Tuple[str, ...]]]] = []

    for night in range(1, problem.nights + 1):
        nxt: Dict[FrozenSet[str], Tuple[Optional[FrozenSet[str]], Tuple[str, ...]]] = {}
        cap = problem.capacity(night)
        for state in frontier:
            remaining = [n for n in names if n not in state and problem.schools[n].can_switch_on(night)]
            max_k = min(cap, len(remaining))
            for k in range(0, max_k + 1):
                for combo in itertools.combinations(remaining, k):
                    new_state = state | frozenset(combo)
                    if new_state in nxt:
                        continue
                    ok, _ = _check_morning(problem, constraints, new_state)
                    if ok:
                        nxt[new_state] = (state, combo)
        layers.append(nxt)
        frontier = nxt
        if all_schools in frontier:
            # We can stop early only if all remaining nights would keep the
            # all-new morning; but we still must fill capacities? No -- nights
            # may be idle. Once all switched and every prior morning checked,
            # later mornings are all-new and were verified by end_ok.
            break

    if all_schools in frontier:
        # reconstruct: walk backpointers from the all-new state to the start.
        chain: List[Tuple[int, Tuple[str, ...], FrozenSet[str]]] = []
        # locate which layer contains all_schools
        layer_idx = None
        for i, layer in enumerate(layers):
            if all_schools in layer:
                layer_idx = i
                break
        assert layer_idx is not None
        cur = all_schools
        for i in range(layer_idx, -1, -1):
            prev, combo = layers[i][cur]
            chain.append((i + 1, combo, cur))
            cur = prev  # type: ignore[assignment]
        chain.reverse()

        calendar: List[NightBatch] = []
        for night, combo, new_state in chain:
            ok, checks = _check_morning(problem, constraints, new_state)
            calendar.append(NightBatch(
                night=night,
                switched=tuple(sorted(combo)),
                versions_after=_versions(new_state, names),
                channels_ok=ok,
                channel_checks=checks,
            ))
        return CalendarResult(
            feasible=True,
            calendar=calendar,
            constraints=constraint_report,
            diagnostics={"reason": "calendar found", "switched_every_school": True},
        )

    # infeasible -- diagnose
    diagnostics = _diagnose(
        problem, constraints, start_ok, end_ok, start_checks, end_checks, names
    )
    _attach_evidence_gaps(diagnostics, constraints)
    return CalendarResult(
        feasible=False,
        calendar=[],
        constraints=constraint_report,
        diagnostics=diagnostics,
    )


# --------------------------------------------------------------------------- #
# infeasibility diagnosis
# --------------------------------------------------------------------------- #
def _attach_evidence_gaps(
    diagnostics: dict, constraints: Dict[Tuple[str, str], EdgeConstraint]
) -> None:
    """Surface missing/stale signatures so an absent form is never silently
    reported as a *proven* broken channel."""
    gaps = [g for ec in constraints.values() for g in ec.evidence_gaps()]
    if gaps:
        diagnostics.setdefault("evidence_gaps", []).extend(gaps)


def _sccs(nodes: Sequence[str], edges: Dict[str, Set[str]]) -> List[Set[str]]:
    """Tarjan-free Kosaraju over a tiny node set."""
    index_counter = [0]
    stack: List[str] = []
    on_stack: Dict[str, bool] = {n: False for n in nodes}
    indices: Dict[str, int] = {}
    low: Dict[str, int] = {}
    result: List[Set[str]] = []
    adj: Dict[str, List[str]] = {n: sorted(edges.get(n, set())) for n in nodes}

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        low[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in adj[v]:
            if w not in indices:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif on_stack[w]:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            comp: Set[str] = set()
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.add(w)
                if w == v:
                    break
            result.append(comp)

    for v in nodes:
        if v not in indices:
            strongconnect(v)
    return result


def _diagnose(
    problem: SwitchProblem,
    constraints: Dict[Tuple[str, str], EdgeConstraint],
    start_ok: bool,
    end_ok: bool,
    start_checks: List[dict],
    end_checks: List[dict],
    names: Sequence[str],
) -> dict:
    blockers: List[dict] = []

    if not start_ok:
        bad = [c for c in start_checks if not c["compatible"]]
        witnesses = []
        for c in bad:
            ec = constraints[(c["channel"][0], c["channel"][1])]
            witnesses.append({
                "channel": c["channel"],
                "versions": c["versions"],
                "witness": ec.witnesses.get("old,old"),
            })
        return {
            "reason": "initial_state_incompatible",
            "message": "the network is already broken before any switch (all schools on old catalogs).",
            "blocking_channels": bad,
            "witnesses": witnesses,
        }
    if not end_ok:
        bad = [c for c in end_checks if not c["compatible"]]
        witnesses = []
        for c in bad:
            ec = constraints[(c["channel"][0], c["channel"][1])]
            witnesses.append({
                "channel": c["channel"],
                "versions": c["versions"],
                "witness": ec.witnesses.get("new,new"),
            })
        return {
            "reason": "final_state_incompatible",
            "message": "even with every school on its new catalog some channel is incompatible.",
            "blocking_channels": bad,
            "witnesses": witnesses,
        }

    def allowed_set(name: str) -> FrozenSet[int]:
        a = problem.schools[name].allowed_nights
        return a if a is not None else frozenset(range(1, problem.nights + 1))

    # Build a "<=" graph: u <= v means u must switch on an earlier-or-equal night.
    le_edges: Dict[str, Set[str]] = {n: set() for n in names}
    order_witness: Dict[Tuple[str, str], Optional[dict]] = {}
    same_night_edges: List[Tuple[str, str]] = []
    for ec in constraints.values():
        kind = ec.kind
        if kind == "same_night":
            le_edges[ec.sender].add(ec.receiver)
            le_edges[ec.receiver].add(ec.sender)
            same_night_edges.append((ec.sender, ec.receiver))
        elif kind == "order_sender_first":
            le_edges[ec.sender].add(ec.receiver)
            order_witness[(ec.sender, ec.receiver)] = ec.witnesses.get("old,new")
        elif kind == "order_receiver_first":
            le_edges[ec.receiver].add(ec.sender)
            order_witness[(ec.receiver, ec.sender)] = ec.witnesses.get("new,old")

    comps = _sccs(names, le_edges)
    comp_of: Dict[str, int] = {}
    for i, comp in enumerate(comps):
        for n in comp:
            comp_of[n] = i

    # (a) forced co-switch (SCC) groups: a shared night with enough capacity
    for comp in comps:
        if len(comp) < 2:
            continue
        comp_names = sorted(comp)
        common = None
        for n in comp_names:
            common = allowed_set(n) if common is None else (common & allowed_set(n))
        common = common or frozenset()
        if not common:
            blockers.append({
                "type": "co_switch_deadline_conflict",
                "message": (f"channels force {comp_names} to switch on the same night, but "
                            f"their available nights have no common night."),
                "group": comp_names,
                "available_nights": {n: sorted(allowed_set(n)) for n in comp_names},
            })
            continue
        viable = [n for n in sorted(common) if problem.capacity(n) >= len(comp)]
        if not viable:
            blockers.append({
                "type": "co_switch_capacity_conflict",
                "message": (f"channels force {comp_names} to switch on the same night, but no "
                            f"common night ({sorted(common)}) has capacity for all {len(comp)} "
                            f"schools (capacities "
                            f"{ {n: problem.capacity(n) for n in sorted(common)} })."),
                "group": comp_names,
                "common_nights": sorted(common),
                "required_capacity": len(comp),
            })

    # (b) ordering deadlines between (condensed) components
    for (u, v), wit in order_witness.items():
        if comp_of[u] == comp_of[v]:
            continue  # same co-switch group, handled above
        if min(allowed_set(u)) > max(allowed_set(v)):
            blockers.append({
                "type": "order_deadline_conflict",
                "message": (f"a channel forces {u} to switch no later than {v}, but {u}'s "
                            f"earliest night {min(allowed_set(u))} is after {v}'s latest "
                            f"{max(allowed_set(v))}."),
                "pair": [u, v],
                "witness": wit,
            })

    # (c) capacity Hall check: schools that must be placed by night t cannot
    # exceed the cumulative capacity of nights 1..t.
    for t in range(1, problem.nights + 1):
        due = [n for n in names if max(allowed_set(n)) <= t]
        cap_cum = sum(problem.capacity(n) for n in range(1, t + 1))
        if len(due) > cap_cum:
            blockers.append({
                "type": "capacity_deadline_conflict",
                "message": (f"by night {t}, {len(due)} schools must already have switched "
                            f"({sorted(due)}) but cumulative night capacity is only {cap_cum}."),
                "night": t,
                "schools_due": sorted(due),
                "cumulative_capacity": cap_cum,
            })

    if blockers:
        return {
            "reason": "constraints_block_calendar",
            "message": "no ordering of single switches keeps every morning exchangeable.",
            "blockers": blockers,
            "co_switch_groups": [sorted(c) for c in comps if len(c) > 1],
        }

    return {
        "reason": "constraint_interaction",
        "message": ("ordering / same-night requirements interact with the per-night "
                    "capacity in a way that admits no calendar; inspect constraints."),
        "co_switch_groups": [sorted(c) for c in comps if len(c) > 1],
    }


__all__ = [
    "School",
    "Channel",
    "SwitchProblem",
    "parse_switch_problem",
    "Booklet",
    "FormLookup",
    "EdgeConstraint",
    "derive_constraints",
    "schedule",
    "CalendarResult",
    "NightBatch",
    "PROBLEM_FORMAT",
    "OLD",
    "NEW",
]

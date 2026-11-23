"""Directed exchange-window network (有向换章窗口).

Each window carries a *whole* voucher naming a source ``(school, kind)`` and a
strictly positive rational ratio.  A whole voucher of integer face value ``v``
sent through a window is floored **once** to ``floor(v * ratio)`` and stamped as
one voucher of the target ``(school, kind)``; a result of ``0`` produces **no**
new voucher, so the walk stops there.

A *multi-issue* (多发) exists when a real ledger pool can start at its own
``(school, kind)`` with face value ``v``, travel whole (never split, merged or
topped up) through at most ``max_steps`` open windows, and return to the *same*
``(school, kind)`` with a strictly larger face value.

Because every step floors independently, a walk whose ratio product exceeds 1
can still fail to grow: a small real face value is rounded down at each stop.
``find_multi_issue`` simulates the floors exactly, so such "profitable on paper
but dead in practice" paths are *not* reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import SchemaError, WindowError
from .model import content_hash
from .rationals import (
    MAX_NODE_KINDS,
    MAX_WINDOWS,
    floor_fraction,
    format_ratio,
    parse_positive_rational,
    validate_steps,
)

WINDOW_FORMAT = "exchange-windows/1"


# --------------------------------------------------------------------------- #
# nodes and windows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, order=True)
class Node:
    school: str
    kind: str

    def as_dict(self) -> dict:
        return {"school": self.school, "kind": self.kind}

    @staticmethod
    def parse(raw: Any, *, location: str) -> "Node":
        if not isinstance(raw, dict):
            raise SchemaError("node must be an object with school and kind", location=location)
        school = raw.get("school")
        kind = raw.get("kind")
        if not isinstance(school, str) or school.strip() == "":
            raise SchemaError("node school must be a non-empty string", location=location)
        if not isinstance(kind, str) or kind.strip() == "":
            raise SchemaError("node kind must be a non-empty string", location=location)
        return Node(school=school, kind=kind)


@dataclass(frozen=True)
class Window:
    window_id: str
    source: Node
    target: Node
    ratio: Fraction
    joint: bool = False

    def as_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "from": self.source.as_dict(),
            "to": self.target.as_dict(),
            "ratio": format_ratio(self.ratio),
            "joint": self.joint,
        }


@dataclass(frozen=True)
class Network:
    revision: int
    windows: Dict[str, Window]

    def __post_init__(self) -> None:
        nodes = set()
        for w in self.windows.values():
            nodes.add(w.source)
            nodes.add(w.target)
        object.__setattr__(self, "_nodes", frozenset(nodes))

    @property
    def nodes(self) -> frozenset:
        return self._nodes  # type: ignore[attr-defined]

    def window(self, window_id: str) -> Window:
        try:
            return self.windows[window_id]
        except KeyError:
            raise WindowError(f"unknown window {window_id!r}", location=window_id) from None

    def out_edges(self, node: Node, open_ids: Optional[Iterable[str]] = None) -> List[Window]:
        allowed = None if open_ids is None else set(open_ids)
        edges = [
            w for w in self.windows.values()
            if w.source == node and (allowed is None or w.window_id in allowed)
        ]
        return sorted(edges, key=lambda w: w.window_id)

    def raw_document(self) -> dict:
        return {
            "format": WINDOW_FORMAT,
            "revision": self.revision,
            "windows": [self.windows[wid].as_dict() for wid in sorted(self.windows)],
        }

    def content_hash(self) -> str:
        return content_hash(self.raw_document())


def parse_network(doc: Any) -> Network:
    if not isinstance(doc, dict):
        raise SchemaError("exchange-window document must be an object")
    if doc.get("format") != WINDOW_FORMAT:
        raise SchemaError(f"window format must be {WINDOW_FORMAT!r}", location="format")
    revision = doc.get("revision")
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1):
        raise SchemaError("window revision must be a positive integer", location="revision")
    raw = doc.get("windows")
    if not isinstance(raw, list) or not raw:
        raise SchemaError("windows must be a non-empty list", location="windows")
    if len(raw) > MAX_WINDOWS:
        raise SchemaError(f"at most {MAX_WINDOWS} windows are supported (got {len(raw)})", location="windows")

    windows: Dict[str, Window] = {}
    nodes = set()
    for idx, entry in enumerate(raw):
        loc = f"windows[{idx}]"
        if not isinstance(entry, dict):
            raise SchemaError("window must be an object", location=loc)
        wid = entry.get("window_id")
        if not isinstance(wid, str) or wid.strip() == "":
            raise SchemaError("window_id must be a non-empty string", location=loc)
        if wid in windows:
            raise SchemaError(f"duplicate window_id {wid!r}", location=wid)
        source = Node.parse(entry.get("from"), location=wid)
        target = Node.parse(entry.get("to"), location=wid)
        ratio = parse_positive_rational(entry.get("ratio"), location=wid)
        joint = bool(entry.get("joint", False))
        windows[wid] = Window(window_id=wid, source=source, target=target, ratio=ratio, joint=joint)
        nodes.add(source)
        nodes.add(target)
    if len(nodes) > MAX_NODE_KINDS:
        raise SchemaError(
            f"at most {MAX_NODE_KINDS} distinct (school, kind) nodes are supported (got {len(nodes)})",
            location="windows",
        )
    return Network(revision=revision, windows=windows)


# --------------------------------------------------------------------------- #
# path simulation
# --------------------------------------------------------------------------- #
def stamp(value: int, ratio: Fraction) -> int:
    """Whole-voucher stamp: floor once, exact rational arithmetic."""
    return floor_fraction(ratio * value)


@dataclass(frozen=True)
class Step:
    window_id: str
    source: Node
    target: Node
    value_in: int
    value_out: int

    def as_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "from": self.source.as_dict(),
            "to": self.target.as_dict(),
            "value_in": self.value_in,
            "value_out": self.value_out,
        }


@dataclass(frozen=True)
class Walk:
    """A concrete traversal with the face value actually stamped at each stop."""

    start_node: Node
    start_value: int
    steps: Tuple[Step, ...]

    @property
    def end_node(self) -> Node:
        return self.steps[-1].target if self.steps else self.start_node

    @property
    def end_value(self) -> int:
        return self.steps[-1].value_out if self.steps else self.start_value

    @property
    def is_multi_issue(self) -> bool:
        return self.end_node == self.start_node and self.end_value > self.start_value

    def as_dict(self) -> dict:
        return {
            "start": {"node": self.start_node.as_dict(), "value": self.start_value},
            "steps": [s.as_dict() for s in self.steps],
            "end": {"node": self.end_node.as_dict(), "value": self.end_value},
            "multi_issue": self.is_multi_issue,
        }


def _dfs(
    network: Network,
    open_ids: frozenset,
    node: Node,
    value: int,
    path: List[Step],
    remaining: int,
    start_node: Node,
    start_value: int,
) -> Optional[List[Step]]:
    if remaining == 0:
        return None
    for w in network.out_edges(node, open_ids):
        out = stamp(value, w.ratio)
        if out == 0:
            # Zero produces no voucher: this branch cannot continue.
            continue
        step = Step(window_id=w.window_id, source=node, target=w.target, value_in=value, value_out=out)
        path.append(step)
        if w.target == start_node and out > start_value:
            return list(path)
        if remaining - 1 > 0:
            found = _dfs(network, open_ids, w.target, out, path, remaining - 1, start_node, start_value)
            if found is not None:
                return found
        path.pop()
    return None


def find_multi_issue(
    network: Network,
    start_node: Node,
    start_value: int,
    *,
    max_steps: int,
    open_ids: Optional[Iterable[str]] = None,
) -> Optional[Walk]:
    """Return the shortest profitable round trip for one real start, or ``None``.

    Searches by strictly increasing step count, so the witness uses the fewest
    windows.  ``open_ids`` limits the walk to currently-open windows.
    """
    validate_steps(max_steps)
    if start_value <= 0:
        return None
    open_set = frozenset(network.windows) if open_ids is None else frozenset(open_ids)
    for depth in range(1, max_steps + 1):
        found = _dfs(network, open_set, start_node, start_value, [], depth, start_node, start_value)
        if found is not None:
            return Walk(start_node=start_node, start_value=start_value, steps=tuple(found))
    return None


# --------------------------------------------------------------------------- #
# batch starts + suspension
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StartState:
    """One real ledger pool: label (for reports), node and stampable face value."""

    label: str
    node: Node
    value: int


@dataclass(frozen=True)
class ProfitFinding:
    label: str
    walk: Walk

    def as_dict(self) -> dict:
        data = self.walk.as_dict()
        data["label"] = self.label
        return data


def find_profits(
    network: Network,
    starts: Sequence[StartState],
    *,
    max_steps: int,
    open_ids: Optional[Iterable[str]] = None,
) -> List[ProfitFinding]:
    findings: List[ProfitFinding] = []
    for st in starts:
        walk = find_multi_issue(network, st.node, st.value, max_steps=max_steps, open_ids=open_ids)
        if walk is not None:
            findings.append(ProfitFinding(label=st.label, walk=walk))
    return findings


def reachable(
    network: Network,
    source: Node,
    target: Node,
    *,
    open_ids: Optional[Iterable[str]] = None,
) -> bool:
    if source == target:
        return True
    open_set = set(network.windows) if open_ids is None else set(open_ids)
    seen = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        for w in network.out_edges(node, open_set):
            if w.target == target:
                return True
            if w.target not in seen:
                seen.add(w.target)
                stack.append(w.target)
    return False


@dataclass(frozen=True)
class SuspensionRequest:
    max_steps: int
    must_stay_reachable: Tuple[Tuple[Node, Node], ...] = ()
    non_suspendable: Tuple[str, ...] = ()
    protect_joint_windows: bool = False

    def protected_windows(self, network: Network) -> frozenset:
        protected = set(self.non_suspendable)
        if self.protect_joint_windows:
            protected |= {wid for wid, w in network.windows.items() if w.joint}
        return frozenset(protected)


def parse_suspension_request(doc: Any) -> SuspensionRequest:
    if not isinstance(doc, dict):
        raise SchemaError("suspension request must be an object")
    max_steps = validate_steps(doc.get("max_steps", 6), location="max_steps")
    raw_pairs = doc.get("must_stay_reachable", [])
    if not isinstance(raw_pairs, list):
        raise SchemaError("must_stay_reachable must be a list", location="must_stay_reachable")
    pairs = []
    for idx, entry in enumerate(raw_pairs):
        loc = f"must_stay_reachable[{idx}]"
        if not isinstance(entry, dict):
            raise SchemaError("reachability pair must be an object", location=loc)
        pairs.append((Node.parse(entry.get("from"), location=loc), Node.parse(entry.get("to"), location=loc)))
    raw_non = doc.get("non_suspendable", [])
    if not isinstance(raw_non, list) or any(not isinstance(x, str) for x in raw_non):
        raise SchemaError("non_suspendable must be a list of window ids", location="non_suspendable")
    return SuspensionRequest(
        max_steps=max_steps,
        must_stay_reachable=tuple(pairs),
        non_suspendable=tuple(raw_non),
        protect_joint_windows=bool(doc.get("protect_joint_windows", False)),
    )


@dataclass
class SuspensionResult:
    feasible: bool
    closed: Tuple[str, ...]
    max_steps: int
    before_profits: List[ProfitFinding]
    after_profits: List[ProfitFinding]
    connectivity: List[dict]
    contradiction: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "max_steps": self.max_steps,
            "closed_windows": list(self.closed),
            "before": [p.as_dict() for p in self.before_profits],
            "after": {
                "profits": [p.as_dict() for p in self.after_profits],
                "connectivity": list(self.connectivity),
            },
            "contradiction": self.contradiction,
        }


def _connectivity(network: Network, open_ids, pairs) -> List[dict]:
    rows = []
    for src, dst in pairs:
        ok = reachable(network, src, dst, open_ids=open_ids)
        rows.append({"from": src.as_dict(), "to": dst.as_dict(), "reachable": ok})
    return rows


def solve_suspension(
    network: Network,
    starts: Sequence[StartState],
    request: SuspensionRequest,
) -> SuspensionResult:
    """Minimum-cardinality window closure that stops every real start multi-issuing.

    Conditions enforced on the post-closure open network:

    * **no multi-issue** for any real ledger start within ``max_steps``;
    * every ``must_stay_reachable`` pair stays directed-reachable (interface
      reachability only -- no face-value simulation).

    Windows in ``non_suspendable`` (and, if requested, ``joint`` windows) cannot
    be closed.  If no compliant closure exists the result carries a
    ``contradiction`` explaining whether reachability, multi-issue, or both are
    impossible to satisfy.
    """
    validate_steps(request.max_steps)
    for wid in request.non_suspendable:
        if wid not in network.windows:
            raise WindowError(f"non_suspendable window {wid!r} is not in the network", location=wid)
    protected = request.protected_windows(network)
    candidate = sorted(wid for wid in network.windows if wid not in protected)

    all_ids = frozenset(network.windows)
    before = find_profits(network, starts, max_steps=request.max_steps, open_ids=all_ids)

    for size in range(0, len(candidate) + 1):
        for combo in combinations(candidate, size):
            closed = set(combo)
            open_ids = frozenset(all_ids - closed)
            conn = _connectivity(network, open_ids, request.must_stay_reachable)
            if not all(c["reachable"] for c in conn):
                continue
            profits = find_profits(network, starts, max_steps=request.max_steps, open_ids=open_ids)
            if profits:
                continue
            return SuspensionResult(
                feasible=True,
                closed=tuple(combo),
                max_steps=request.max_steps,
                before_profits=before,
                after_profits=[],
                connectivity=conn,
            )

    # No compliant closure.  Diagnose.
    # Reachability is *best* when every window stays open, so that is the test
    # for "reachability cannot be satisfied at all".  Multi-issue is *worst*
    # when only the protected windows remain, so that is the test for
    # "multi-issue cannot be avoided".
    conn_all_open = _connectivity(network, all_ids, request.must_stay_reachable)
    reachability_alone_impossible = not all(c["reachable"] for c in conn_all_open)

    open_protected = frozenset(protected)
    residual = find_profits(network, starts, max_steps=request.max_steps, open_ids=open_protected)
    multi_issue_unavoidable = bool(residual)

    if reachability_alone_impossible and multi_issue_unavoidable:
        message = (
            "no compliant suspension: reachability pairs cannot be preserved even with every "
            "window open, and the protected (non-suspendable) windows alone still multi-issue"
        )
    elif reachability_alone_impossible:
        message = (
            "no compliant suspension: the required reachability pairs have no direction-respecting "
            "path even with every window open (non-suspendable windows block the only route)"
        )
    elif multi_issue_unavoidable:
        message = (
            "no compliant suspension: closing every closable window still leaves a multi-issue "
            "path on the protected (non-suspendable) windows"
        )
    else:
        message = (
            "no compliant suspension: multi-issue prevention and reachability are incompatible -- "
            "every closure that stops multi-issue breaks a required reachability pair, while every "
            "closure that preserves reachability leaves a multi-issue"
        )

    return SuspensionResult(
        feasible=False,
        closed=(),
        max_steps=request.max_steps,
        before_profits=before,
        after_profits=residual,
        connectivity=_connectivity(network, open_protected, request.must_stay_reachable),
        contradiction={
            "message": message,
            "reachability_impossible_even_fully_open": reachability_alone_impossible,
            "multi_issue_on_protected_windows": multi_issue_unavoidable,
            "protected_windows": sorted(protected),
            "residual_profits": [p.as_dict() for p in residual],
        },
    )


__all__ = [
    "Node",
    "Window",
    "Network",
    "parse_network",
    "Walk",
    "Step",
    "StartState",
    "ProfitFinding",
    "SuspensionRequest",
    "SuspensionResult",
    "parse_suspension_request",
    "stamp",
    "find_multi_issue",
    "find_profits",
    "reachable",
    "solve_suspension",
    "WINDOW_FORMAT",
]

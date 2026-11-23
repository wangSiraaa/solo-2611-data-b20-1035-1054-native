"""Offline case handover between schools and the inter-university league.

The real credit ledgers cannot leave their school; the league holds the window
agreement.  After the hearing the two sides may be offline, yet the *same case
handover packet* must still let a reviewer either

* **find a real multi-issue** (a concrete face-value walk that returns larger), or
* **confirm coverage**: after the suspension, every whole face value actually
  issued in the batch is accounted for (no walk can multi-issue it).

Chosen handover method: **anonymised batch roster + league coverage receipt**
(the roster method).  Schools publish a de-identified roster of actually issued
``(school, kind, whole face value)`` rows, merging equal face values and keeping
only an in-school cross-reference number per holder.  The league replays the
whole-voucher stamping rules against that roster and returns a receipt that,
per face value, either carries the real path or marks it covered.

Why the appeal and the hearing agree
------------------------------------
A face value's multi-issue status depends only on ``(school, kind, face_value,
open windows, max_steps)`` -- never on *who* holds it.  Merging equal face
values and stripping holder identity therefore cannot change the ruling.  So an
in-school appeal computed from the real ledger and the league's hearing computed
from the anonymised roster reach the same verdict for every face value.

Cancellation / re-issuance are roster edits: adding a holder (re-issue) or
removing one (cancel).  A face value is *issued* exactly while it has at least
one holder, so the last cancellation drops the row and the coverage receipt
shrinks with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .errors import HandoverError, SchemaError
from .ledger import Ledger
from .model import content_hash
from .windows import (
    Network,
    Node,
    Walk,
    find_multi_issue,
)

ROSTER_FORMAT = "credit-roster/1"
PACKET_FORMAT = "case-handover/1"

FaceKey = Tuple[str, str, int]  # (school, kind, face_value)


# --------------------------------------------------------------------------- #
# anonymised batch roster
# --------------------------------------------------------------------------- #
@dataclass
class RosterEntry:
    school: str
    kind: str
    face_value: int
    holders: List[str] = field(default_factory=list)   # opaque in-school refs

    @property
    def key(self) -> FaceKey:
        return (self.school, self.kind, self.face_value)

    @property
    def node(self) -> Node:
        return Node(self.school, self.kind)

    @property
    def holder_count(self) -> int:
        return len(self.holders)

    def as_dict(self) -> dict:
        return {
            "school": self.school,
            "kind": self.kind,
            "face_value": self.face_value,
            "holders": sorted(self.holders),
        }


@dataclass
class Roster:
    revision: int
    entries: Dict[FaceKey, RosterEntry] = field(default_factory=dict)

    # -- edits ------------------------------------------------------------
    def add_holder(self, school: str, kind: str, face_value: int, holder_ref: str) -> None:
        if face_value <= 0:
            raise HandoverError("a roster face value must be a positive whole number", location=f"{school}:{kind}")
        key = (school, kind, face_value)
        entry = self.entries.get(key)
        if entry is None:
            entry = RosterEntry(school=school, kind=kind, face_value=face_value)
            self.entries[key] = entry
        if holder_ref not in entry.holders:
            entry.holders.append(holder_ref)

    def remove_holder(self, school: str, kind: str, face_value: int, holder_ref: str) -> bool:
        key = (school, kind, face_value)
        entry = self.entries.get(key)
        if entry is None or holder_ref not in entry.holders:
            return False
        entry.holders.remove(holder_ref)
        if not entry.holders:
            # No holder left -> the face value is no longer issued.
            del self.entries[key]
        return True

    def has_holders(self, school: str, kind: str, face_value: int) -> bool:
        entry = self.entries.get((school, kind, face_value))
        return bool(entry and entry.holders)

    def relabel_revision(self, revision: int) -> "Roster":
        return Roster(revision=revision, entries={k: e for k, e in self.entries.items()})

    # -- serialisation ----------------------------------------------------
    def faces(self) -> List[RosterEntry]:
        return [self.entries[k] for k in sorted(self.entries)]

    def raw_document(self) -> dict:
        return {
            "format": ROSTER_FORMAT,
            "revision": self.revision,
            "entries": [e.as_dict() for e in self.faces()],
        }

    def content_hash(self) -> str:
        return content_hash(self.raw_document())


def parse_roster(doc: object) -> Roster:
    if not isinstance(doc, dict):
        raise SchemaError("roster must be an object")
    if doc.get("format") != ROSTER_FORMAT:
        raise SchemaError(f"roster format must be {ROSTER_FORMAT!r}", location="format")
    revision = doc.get("revision")
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1):
        raise SchemaError("roster revision must be a positive integer", location="revision")
    raw = doc.get("entries")
    if not isinstance(raw, list):
        raise SchemaError("roster entries must be a list", location="entries")
    roster = Roster(revision=revision)
    for idx, e in enumerate(raw):
        loc = f"entries[{idx}]"
        if not isinstance(e, dict):
            raise SchemaError("roster entry must be an object", location=loc)
        school = e.get("school")
        kind = e.get("kind")
        fv = e.get("face_value")
        holders = e.get("holders", [])
        if not isinstance(school, str) or not school.strip():
            raise SchemaError("roster entry school must be a non-empty string", location=loc)
        if not isinstance(kind, str) or not kind.strip():
            raise SchemaError("roster entry kind must be a non-empty string", location=loc)
        if not (isinstance(fv, int) and not isinstance(fv, bool) and fv >= 1):
            raise SchemaError("roster face_value must be a positive integer", location=loc)
        if not isinstance(holders, list) or any(not isinstance(h, str) or not h.strip() for h in holders):
            raise SchemaError("roster holders must be a list of non-empty strings", location=loc)
        if not holders:
            raise SchemaError("a roster face value must retain at least one holder", location=loc)
        for href in holders:
            roster.add_holder(school, kind, fv, href)
    return roster


def roster_from_ledgers(items: Iterable[Tuple[Ledger, str]], *, revision: int = 1) -> Roster:
    """Build an anonymised roster from real ledgers.

    ``items`` pairs each ledger with the school's opaque cross-reference number
    for its holder.  Equal ``(school, kind, whole face value)`` rows merge.  A
    pool that rounds to zero whole credits issues no whole face value and is
    therefore absent (its fractional tail stays on the ledger, not the roster).
    """
    roster = Roster(revision=revision)
    for ledger, holder_ref in items:
        for pool in ledger.pools.values():
            if pool.stampable <= 0:
                continue
            roster.add_holder(pool.issuing_school, pool.kind, pool.stampable, holder_ref)
    return roster


# --------------------------------------------------------------------------- #
# coverage receipt (league side replay of the stamping rules)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FaceJudgement:
    school: str
    kind: str
    face_value: int
    multi_issue: bool
    walk: Optional[Walk]
    holder_count: int

    @property
    def key(self) -> FaceKey:
        return (self.school, self.kind, self.face_value)

    def as_dict(self) -> dict:
        return {
            "school": self.school,
            "kind": self.kind,
            "face_value": self.face_value,
            "status": "multi_issue" if self.multi_issue else "covered",
            "holder_count": self.holder_count,
            "path": self.walk.as_dict() if self.walk else None,
        }


@dataclass
class CoverageReceipt:
    max_steps: int
    closed_windows: Tuple[str, ...]
    network_revision: int
    roster_hash: str
    judgements: List[FaceJudgement] = field(default_factory=list)

    @property
    def all_covered(self) -> bool:
        return all(not j.multi_issue for j in self.judgements)

    @property
    def violations(self) -> List[FaceJudgement]:
        return [j for j in self.judgements if j.multi_issue]

    @property
    def outcome(self) -> str:
        return "coverage" if self.all_covered else "multi_issue_found"

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "all_covered": self.all_covered,
            "max_steps": self.max_steps,
            "closed_windows": list(self.closed_windows),
            "network_revision": self.network_revision,
            "roster_hash": self.roster_hash,
            "face_values": [j.as_dict() for j in self.judgements],
        }


def coverage_receipt(
    roster: Roster,
    network: Network,
    *,
    max_steps: int,
    closed_windows: Iterable[str] = (),
) -> CoverageReceipt:
    """Replay whole-voucher stamping against every issued roster face value.

    Open windows are all windows minus ``closed_windows``.  Each row records
    either the concrete multi-issue path or that the face value is covered.
    """
    closed = tuple(sorted(set(closed_windows)))
    for wid in closed:
        if wid not in network.windows:
            raise HandoverError(f"closed window {wid!r} is not in the network", location=wid)
    open_ids = frozenset(set(network.windows) - set(closed))

    judgements: List[FaceJudgement] = []
    for entry in roster.faces():
        walk = find_multi_issue(
            network, entry.node, entry.face_value, max_steps=max_steps, open_ids=open_ids
        )
        judgements.append(FaceJudgement(
            school=entry.school,
            kind=entry.kind,
            face_value=entry.face_value,
            multi_issue=walk is not None,
            walk=walk,
            holder_count=entry.holder_count,
        ))
    return CoverageReceipt(
        max_steps=max_steps,
        closed_windows=closed,
        network_revision=network.revision,
        roster_hash=roster.content_hash(),
        judgements=judgements,
    )


# --------------------------------------------------------------------------- #
# in-school appeal vs league hearing consistency
# --------------------------------------------------------------------------- #
def ledger_judgements(
    ledger: Ledger,
    network: Network,
    *,
    max_steps: int,
    closed_windows: Iterable[str] = (),
) -> List[FaceJudgement]:
    """In-school view: judge each *real* pool face value (one holder each)."""
    open_ids = frozenset(set(network.windows) - set(closed_windows))
    rows: List[FaceJudgement] = []
    for key in sorted(ledger.pools):
        pool = ledger.pools[key]
        if pool.stampable <= 0:
            continue
        node = Node(pool.issuing_school, pool.kind)
        walk = find_multi_issue(network, node, pool.stampable, max_steps=max_steps, open_ids=open_ids)
        rows.append(FaceJudgement(
            school=pool.issuing_school,
            kind=pool.kind,
            face_value=pool.stampable,
            multi_issue=walk is not None,
            walk=walk,
            holder_count=1,
        ))
    return rows


def compare_ledger_and_roster(
    ledger_items: Iterable[Tuple[Ledger, str]],
    roster: Roster,
    network: Network,
    *,
    max_steps: int,
    closed_windows: Iterable[str] = (),
) -> dict:
    """Assert the in-school appeal and league hearing reach the same ruling.

    Grouping real ledgers by face value must reproduce exactly the roster rows,
    and every ledger's per-pool ``multi_issue`` flag must equal the roster
    receipt's flag for the same ``(school, kind, face_value)``.  Returns a
    consistency report; ``consistent`` is true iff all rows agree.
    """
    closed = tuple(closed_windows)
    roster_receipt = coverage_receipt(roster, network, max_steps=max_steps, closed_windows=closed)
    league = {j.key: j for j in roster_receipt.judgements}

    mismatches: List[dict] = []
    checked = 0
    for ledger, holder_ref in ledger_items:
        in_school_rows = ledger_judgements(ledger, network, max_steps=max_steps, closed_windows=closed)
        for row in in_school_rows:
            checked += 1
            lg = league.get(row.key)
            if lg is None:
                mismatches.append({
                    "key": {"school": row.school, "kind": row.kind, "face_value": row.face_value},
                    "reason": "face value missing from league roster",
                    "holder_ref": holder_ref,
                })
                continue
            if lg.multi_issue != row.multi_issue:
                mismatches.append({
                    "key": {"school": row.school, "kind": row.kind, "face_value": row.face_value},
                    "reason": "in-school and league rulings differ",
                    "in_school_multi_issue": row.multi_issue,
                    "league_multi_issue": lg.multi_issue,
                    "holder_ref": holder_ref,
                })
    return {
        "consistent": not mismatches,
        "checked": checked,
        "mismatches": mismatches,
        "league_outcome": roster_receipt.outcome,
    }


# --------------------------------------------------------------------------- #
# ratio-revision diff: which old judgements still apply
# --------------------------------------------------------------------------- #
def multi_issue_thresholds(
    network: Network,
    node: Node,
    *,
    max_steps: int,
    up_to: int,
    closed_windows: Iterable[str] = (),
) -> List[int]:
    """Whole face values ``1..up_to`` that multi-issue from ``node``.

    This is the league-side integer threshold Table: the exact set of face
    values a start would exploit within ``max_steps``.  Recompute it whenever a
    window ratio changes and diff the two sets.
    """
    open_ids = frozenset(set(network.windows) - set(closed_windows))
    bad: List[int] = []
    for value in range(1, up_to + 1):
        if find_multi_issue(network, node, value, max_steps=max_steps, open_ids=open_ids) is not None:
            bad.append(value)
    return bad


def diff_judgements(old: CoverageReceipt, new: CoverageReceipt) -> dict:
    """Determine which old face-value rulings still apply after a ratio change.

    Both receipts are keyed by ``(school, kind, face_value)``.  A ruling *still
    applies* when the ``multi_issue`` flag is unchanged.  Newly issuing and
    newly cleared face values are listed separately, and face values present in
    only one receipt are reported as added / removed.
    """
    old_map: Dict[FaceKey, bool] = {j.key: j.multi_issue for j in old.judgements}
    new_map: Dict[FaceKey, bool] = {j.key: j.multi_issue for j in new.judgements}

    still_applies: List[FaceKey] = []
    newly_multi_issue: List[FaceKey] = []
    newly_covered: List[FaceKey] = []
    added: List[FaceKey] = []
    removed: List[FaceKey] = []

    for key, new_flag in new_map.items():
        if key not in old_map:
            added.append(key)
            continue
        old_flag = old_map[key]
        if old_flag == new_flag:
            still_applies.append(key)
        elif new_flag and not old_flag:
            newly_multi_issue.append(key)
        else:
            newly_covered.append(key)

    for key in old_map:
        if key not in new_map:
            removed.append(key)

    def fmt(keys: List[FaceKey]) -> List[dict]:
        return [{"school": s, "kind": k, "face_value": v} for (s, k, v) in sorted(keys)]

    return {
        "still_applies": fmt(still_applies),
        "newly_multi_issue": fmt(newly_multi_issue),
        "newly_covered": fmt(newly_covered),
        "added": fmt(added),
        "removed": fmt(removed),
        "changed": bool(newly_multi_issue or newly_covered or added or removed),
    }


# --------------------------------------------------------------------------- #
# self-contained case handover packet
# --------------------------------------------------------------------------- #
@dataclass
class HandoverPacket:
    """Everything an offline reviewer needs: roster + window charter + receipt."""

    roster: Roster
    network: Network
    max_steps: int
    closed_windows: Tuple[str, ...]
    receipt: CoverageReceipt

    def as_dict(self) -> dict:
        return {
            "format": PACKET_FORMAT,
            "roster": self.roster.raw_document(),
            "windows": self.network.raw_document(),
            "charter": {
                "max_steps": self.max_steps,
                "closed_windows": list(self.closed_windows),
            },
            "receipt": self.receipt.as_dict(),
        }

    def content_hash(self) -> str:
        return content_hash(self.as_dict())


def build_packet(
    roster: Roster,
    network: Network,
    *,
    max_steps: int,
    closed_windows: Iterable[str] = (),
) -> HandoverPacket:
    closed = tuple(sorted(set(closed_windows)))
    receipt = coverage_receipt(roster, network, max_steps=max_steps, closed_windows=closed)
    return HandoverPacket(
        roster=roster,
        network=network,
        max_steps=max_steps,
        closed_windows=closed,
        receipt=receipt,
    )


def review_packet(packet_doc: object) -> dict:
    """Re-verify a handover packet offline and state the outcome.

    Recomputes the coverage receipt from the embedded roster + window charter so
    a reviewer never has to trust the shipped receipt.  Returns either the real
    multi-issue paths found or a coverage confirmation over all issued face
    values.  Also flags whether the shipped receipt matches the recomputation.
    """
    if not isinstance(packet_doc, dict) or packet_doc.get("format") != PACKET_FORMAT:
        raise HandoverError(f"packet format must be {PACKET_FORMAT!r}", location="format")
    roster = parse_roster(packet_doc.get("roster"))
    network = _parse_network_embedded(packet_doc.get("windows"))
    charter = packet_doc.get("charter") or {}
    max_steps = charter.get("max_steps")
    if not (isinstance(max_steps, int) and not isinstance(max_steps, bool) and max_steps >= 1):
        raise HandoverError("packet charter.max_steps must be a positive integer", location="max_steps")
    closed = charter.get("closed_windows", [])
    if not isinstance(closed, list):
        raise HandoverError("packet charter.closed_windows must be a list", location="closed_windows")

    receipt = coverage_receipt(roster, network, max_steps=max_steps, closed_windows=closed)
    shipped = packet_doc.get("receipt")
    shipped_matches = (
        isinstance(shipped, dict)
        and shipped.get("outcome") == receipt.outcome
        and shipped.get("roster_hash") == receipt.roster_hash
    )
    return {
        "outcome": receipt.outcome,
        "all_covered": receipt.all_covered,
        "checked_face_values": len(receipt.judgements),
        "violations": [j.as_dict() for j in receipt.violations],
        "receipt": receipt.as_dict(),
        "shipped_receipt_matches": shipped_matches,
    }


def _parse_network_embedded(doc: object) -> Network:
    # Deferred import to avoid a hard cycle at module load.
    from .windows import parse_network

    return parse_network(doc)


__all__ = [
    "Roster",
    "RosterEntry",
    "FaceKey",
    "parse_roster",
    "roster_from_ledgers",
    "FaceJudgement",
    "CoverageReceipt",
    "coverage_receipt",
    "ledger_judgements",
    "compare_ledger_and_roster",
    "multi_issue_thresholds",
    "diff_judgements",
    "HandoverPacket",
    "build_packet",
    "review_packet",
    "ROSTER_FORMAT",
    "PACKET_FORMAT",
]

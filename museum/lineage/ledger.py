"""Seal lineage and whole-trip reconciliation (借展谱系与总馆核销).

A *seal* (封) is a named, sealed bag carrying declared per-period coin counts.
The lending library sends named original seals out with an outbound ledger;
branches may:

* forward a whole seal without opening it (no split authorization needed);
* perform one authorized split/merge handover, listing the old seals given
  back, every new seal made in the same batch, each new seal's named holder
  and its declared per-period counts.

Old seals may be handed back exactly once, and only by their current holder;
per-period counts must balance separately across each batch; without opening
authorization a branch can only forward.  Branches may also attach a reweigh
net-weight interval to a seal they currently hold.

The ledger is a pure, persistence-free domain object.  Counters snapshot it to
SQLite and exchange snapshots in offline messages, which keeps this module
unit-testable and independent of transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from museum.weights import WeightCSP, WeightError


class LineageError(Exception):
    """A handover, reweigh, surrender or reconciliation rule was violated."""


def _clean_counts(counts: Mapping[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for period, count in counts.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise LineageError(f"count for period {period!r} must be a non-negative integer")
        if count:
            out[period] = count
    return dict(sorted(out.items()))


def _clean_coin_ids(ids: Sequence[str]) -> list[str]:
    out = sorted(set(ids))
    if len(out) != len(list(ids)):
        raise LineageError("duplicate coin id inside one seal")
    return out


@dataclass
class Seal:
    seal_id: str
    holder: str
    contents: dict[str, int]
    coin_ids: dict[str, list[str]]
    opened: bool = False
    surrendered: bool = False
    returned_to_hq: bool = False
    parent_batch: str | None = None
    birth_seq: int = 0

    def total_count(self) -> int:
        return sum(self.contents.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "seal_id": self.seal_id,
            "holder": self.holder,
            "contents": dict(self.contents),
            "coin_ids": {p: list(ids) for p, ids in sorted(self.coin_ids.items())},
            "opened": self.opened,
            "surrendered": self.surrendered,
            "returned_to_hq": self.returned_to_hq,
            "parent_batch": self.parent_batch,
            "birth_seq": self.birth_seq,
        }


@dataclass
class Batch:
    batch_id: str
    kind: str  # dispatch | forward | split_merge
    actor: str
    old_seal_ids: list[str]
    new_seals: list[dict[str, Any]]
    authorized_open: bool
    seq: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "kind": self.kind,
            "actor": self.actor,
            "old_seal_ids": list(self.old_seal_ids),
            "new_seals": [dict(s) for s in self.new_seals],
            "authorized_open": self.authorized_open,
            "seq": self.seq,
        }


@dataclass
class Reweigh:
    reweigh_id: str
    seal_id: str
    branch: str
    lo: int
    hi: int
    seq: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "reweigh_id": self.reweigh_id,
            "seal_id": self.seal_id,
            "branch": self.branch,
            "lo": self.lo,
            "hi": self.hi,
            "seq": self.seq,
        }


class LineageLedger:
    """In-memory lineage state; every mutator validates before it changes."""

    def __init__(self) -> None:
        self.seals: dict[str, Seal] = {}
        self.batches: list[Batch] = []
        self.reweighs: list[Reweigh] = []
        self.outbound: dict[str, dict[str, int]] = {}
        self._seq = 0
        # coin id -> seal id that currently holds it (double-spend guard)
        self._coin_location: dict[str, str] = {}
        self._batch_ids: set[str] = set()
        self._reweigh_ids: set[str] = set()

    # ------------------------------------------------------------- snapshots
    def snapshot(self) -> dict[str, Any]:
        return {
            "format": "museum-lineage/1",
            "seq": self._seq,
            "outbound": {k: dict(v) for k, v in sorted(self.outbound.items())},
            "seals": {sid: seal.as_dict() for sid, seal in sorted(self.seals.items())},
            "batches": [b.as_dict() for b in self.batches],
            "reweighs": [r.as_dict() for r in self.reweighs],
            "coin_location": dict(sorted(self._coin_location.items())),
        }

    @classmethod
    def restore(cls, data: Mapping[str, Any]) -> "LineageLedger":
        ledger = cls()
        ledger._seq = int(data.get("seq", 0))
        ledger.outbound = {k: dict(v) for k, v in data.get("outbound", {}).items()}
        for sid, raw in data.get("seals", {}).items():
            ledger.seals[sid] = Seal(
                seal_id=raw["seal_id"],
                holder=raw["holder"],
                contents={p: int(c) for p, c in raw["contents"].items()},
                coin_ids={p: list(ids) for p, ids in raw.get("coin_ids", {}).items()},
                opened=bool(raw.get("opened", False)),
                surrendered=bool(raw.get("surrendered", False)),
                returned_to_hq=bool(raw.get("returned_to_hq", False)),
                parent_batch=raw.get("parent_batch"),
                birth_seq=int(raw.get("birth_seq", 0)),
            )
        for raw in data.get("batches", []):
            batch = Batch(
                batch_id=raw["batch_id"],
                kind=raw["kind"],
                actor=raw["actor"],
                old_seal_ids=list(raw["old_seal_ids"]),
                new_seals=[dict(s) for s in raw["new_seals"]],
                authorized_open=bool(raw["authorized_open"]),
                seq=int(raw["seq"]),
            )
            ledger.batches.append(batch)
            ledger._batch_ids.add(batch.batch_id)
        for raw in data.get("reweighs", []):
            ledger.reweighs.append(
                Reweigh(
                    reweigh_id=raw["reweigh_id"],
                    seal_id=raw["seal_id"],
                    branch=raw["branch"],
                    lo=int(raw["lo"]),
                    hi=int(raw["hi"]),
                    seq=int(raw["seq"]),
                )
            )
            ledger._reweigh_ids.add(raw["reweigh_id"])
        ledger._coin_location = dict(data.get("coin_location", {}))
        return ledger

    # ------------------------------------------------------------- internals
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _require_batch_id(self, batch_id: str) -> None:
        if not batch_id:
            raise LineageError("batch id must be a non-empty string")
        if batch_id in self._batch_ids:
            raise LineageError(f"batch {batch_id!r} already recorded (replay must use the same content)")

    def _get_live(self, seal_id: str) -> Seal:
        seal = self.seals.get(seal_id)
        if seal is None:
            raise LineageError(f"unknown seal {seal_id!r}")
        if seal.surrendered:
            raise LineageError(
                f"seal {seal_id!r} was already handed back; an old seal can be surrendered only once"
            )
        return seal

    @staticmethod
    def _sum_counts(seals: Sequence[Seal]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for seal in seals:
            for period, count in seal.contents.items():
                totals[period] = totals.get(period, 0) + count
        return dict(sorted(totals.items()))

    def _check_coin_integrity(self, seals: Sequence[Seal]) -> None:
        seen: dict[str, str] = {}
        for seal in seals:
            declared = sum(seal.contents.values())
            listed = sum(len(ids) for ids in seal.coin_ids.values())
            if declared != listed:
                raise LineageError(
                    f"seal {seal.seal_id!r}: {declared} coins declared but {listed} coin ids listed"
                )
            for period, ids in seal.coin_ids.items():
                if len(ids) != seal.contents.get(period, 0):
                    raise LineageError(
                        f"seal {seal.seal_id!r}: coin ids for {period!r} do not match its count"
                    )
                for cid in ids:
                    if cid in seen:
                        raise LineageError(
                            f"coin {cid!r} appears in both {seen[cid]!r} and {seal.seal_id!r}"
                        )
                    seen[cid] = seal.seal_id

    # ------------------------------------------------------------- dispatch
    def dispatch(
        self,
        batch_id: str,
        seal_id: str,
        holder: str,
        contents: Mapping[str, int],
        coin_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> Seal:
        """Record one original seal leaving the lending library."""

        self._require_batch_id(batch_id)
        if seal_id in self.seals:
            raise LineageError(f"seal {seal_id!r} already exists")
        clean = _clean_counts(contents)
        if not clean:
            raise LineageError(f"original seal {seal_id!r} cannot be empty")
        ids = self._mint_or_check_ids(seal_id, clean, coin_ids)
        seq = self._next_seq()
        seal = Seal(
            seal_id=seal_id,
            holder=holder,
            contents=clean,
            coin_ids=ids,
            parent_batch=batch_id,
            birth_seq=seq,
        )
        self._check_coin_integrity([seal])
        self.seals[seal_id] = seal
        self.outbound[seal_id] = dict(clean)
        self._batch_ids.add(batch_id)
        self.batches.append(
            Batch(batch_id, "dispatch", "HQ", [], [seal.as_dict()], False, seq)
        )
        for cid in self._all_ids(ids):
            self._coin_location[cid] = seal_id
        return seal

    def _mint_or_check_ids(
        self,
        seal_id: str,
        contents: Mapping[str, int],
        coin_ids: Mapping[str, Sequence[str]] | None,
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for period, count in contents.items():
            if coin_ids and period in coin_ids:
                ids = _clean_coin_ids(coin_ids[period])
                if len(ids) != count:
                    raise LineageError(
                        f"seal {seal_id!r}: {count} {period} coins but {len(ids)} ids"
                    )
            else:
                prefix = f"{seal_id}:{period}"
                ids = [f"{prefix}#{i + 1}" for i in range(count)]
            result[period] = ids
        for cid in self._all_ids(result):
            if cid in self._coin_location:
                raise LineageError(f"coin id {cid!r} is already tracked in another seal")
        return dict(sorted(result.items()))

    @staticmethod
    def _all_ids(coin_ids: Mapping[str, Sequence[str]]) -> list[str]:
        return [cid for ids in coin_ids.values() for cid in ids]

    # ------------------------------------------------------------- handovers
    def forward(
        self, batch_id: str, seal_id: str, from_branch: str, to_branch: str
    ) -> Seal:
        """Whole-seal forwarding without opening it."""

        self._require_batch_id(batch_id)
        seal = self._get_live(seal_id)
        if seal.holder != from_branch:
            raise LineageError(
                f"branch {from_branch!r} is not the current holder of {seal_id!r} "
                f"(holder is {seal.holder!r})"
            )
        if from_branch == to_branch:
            raise LineageError("forwarding requires a different destination branch")
        seq = self._next_seq()
        seal.holder = to_branch
        self._batch_ids.add(batch_id)
        self.batches.append(
            Batch(batch_id, "forward", from_branch, [seal_id], [seal.as_dict()], False, seq)
        )
        return seal

    def split_merge(
        self,
        batch_id: str,
        actor: str,
        old_seal_ids: Sequence[str],
        new_seals: Sequence[Mapping[str, Any]],
        *,
        authorized_open: bool,
    ) -> list[Seal]:
        """One authorized handover: surrender old seals, make all new seals.

        ``new_seals`` entries are ``{seal_id, holder, contents, coin_ids?}``.
        Conservation, single surrender, holder authority, no-double-spend and
        the open-authorization rule are all checked before any state changes.
        """

        self._require_batch_id(batch_id)
        if not authorized_open:
            raise LineageError(
                f"batch {batch_id!r}: split/merge needs opening authorization; "
                "without it the branch may only forward seals whole"
            )
        if not old_seal_ids:
            raise LineageError("a split/merge batch must surrender at least one old seal")
        if len(set(old_seal_ids)) != len(list(old_seal_ids)):
            raise LineageError("an old seal cannot be listed twice in one handover")
        old_seals = [self._get_live(sid) for sid in old_seal_ids]
        for seal in old_seals:
            if seal.holder != actor:
                raise LineageError(
                    f"seal {seal.seal_id!r} is held by {seal.holder!r}, not {actor!r}; "
                    "only the current holder may hand it back"
                )
        if not new_seals:
            raise LineageError("a split/merge batch must create at least one new seal")

        before = self._sum_counts(old_seals)
        # The physical coins do not change at a handover: new seals must
        # carry the very same coin ids, redistributed by period.  Callers may
        # state the allocation explicitly; otherwise the ids are handed out
        # deterministically from the surrendered seals' own id pools.
        pool: dict[str, list[str]] = {}
        for seal in old_seals:
            for period, ids in seal.coin_ids.items():
                pool.setdefault(period, []).extend(ids)
        for period in pool:
            pool[period] = sorted(pool[period])
        pool_cursor = {period: 0 for period in pool}

        projected: dict[str, int] = {}
        created: list[Seal] = []
        seq = self._next_seq()
        for raw in new_seals:
            sid = raw["seal_id"]
            if sid in self.seals:
                raise LineageError(f"new seal id {sid!r} already exists")
            holder = raw["holder"]
            contents = _clean_counts(raw.get("contents", {}))
            if not contents:
                raise LineageError(f"new seal {sid!r} cannot be empty")
            if set(contents) - set(before):
                raise LineageError(
                    f"new seal {sid!r} declares a period absent from the surrendered seals"
                )
            if raw.get("coin_ids"):
                ids = {p: _clean_coin_ids(v) for p, v in raw["coin_ids"].items()}
                for period, id_list in ids.items():
                    if len(id_list) != contents.get(period, 0):
                        raise LineageError(
                            f"new seal {sid!r}: coin ids for {period!r} do not match its count"
                        )
                    unknown = set(id_list) - set(pool.get(period, []))
                    if unknown:
                        raise LineageError(
                            f"new seal {sid!r} allocates coins not surrendered in {period!r}: "
                            f"{sorted(unknown)[:3]}"
                        )
            else:
                ids = {}
                for period, count in contents.items():
                    start = pool_cursor[period]
                    ids[period] = pool[period][start:start + count]
                    pool_cursor[period] = start + count
            seal = Seal(
                seal_id=sid,
                holder=holder,
                contents=contents,
                coin_ids=dict(sorted(ids.items())),
                opened=True,
                parent_batch=batch_id,
                birth_seq=seq,
            )
            created.append(seal)
            for period, count in contents.items():
                projected[period] = projected.get(period, 0) + count

        if projected != before:
            raise LineageError(
                "per-period conservation failed: "
                f"surrendered {before} but new seals carry {dict(sorted(projected.items()))}"
            )
        self._check_coin_integrity(created)

        # All coin ids in the new seals must be exactly the coins surrendered.
        old_coin_set = set(self._all_ids_seals(old_seals))
        new_coin_set = set(self._all_ids_seals(created))
        if old_coin_set != new_coin_set:
            missing = old_coin_set - new_coin_set
            extra = new_coin_set - old_coin_set
            raise LineageError(
                "coin-level conservation failed: "
                f"missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}"
            )

        # Commit: old seals die once, new seals are born.
        for seal in old_seals:
            seal.surrendered = True
            seal.opened = True
            for cid in self._all_ids(seal.coin_ids):
                self._coin_location.pop(cid, None)
        for seal in created:
            self.seals[seal.seal_id] = seal
            for cid in self._all_ids(seal.coin_ids):
                if cid in self._coin_location:
                    raise LineageError(
                        f"coin {cid!r} would be booked twice (double spending)"
                    )
                self._coin_location[cid] = seal.seal_id
        self._batch_ids.add(batch_id)
        self.batches.append(
            Batch(
                batch_id,
                "split_merge",
                actor,
                list(old_seal_ids),
                [s.as_dict() for s in created],
                True,
                seq,
            )
        )
        return created

    @staticmethod
    def _all_ids_seals(seals: Sequence[Seal]) -> list[str]:
        return [cid for seal in seals for ids in seal.coin_ids.values() for cid in ids]

    # ------------------------------------------------------------- reweigh
    def reweigh(
        self,
        reweigh_id: str,
        seal_id: str,
        branch: str,
        interval: tuple[int, int],
    ) -> Reweigh:
        if not reweigh_id or reweigh_id in self._reweigh_ids:
            raise LineageError(f"reweigh id {reweigh_id!r} missing or reused")
        lo, hi = interval
        if not isinstance(lo, int) or not isinstance(hi, int) or lo < 0 or lo > hi:
            raise LineageError("reweigh interval must be 0 <= lo <= hi integers")
        seal = self._get_live(seal_id)
        if seal.holder != branch:
            raise LineageError(
                f"branch {branch!r} cannot reweigh {seal_id!r}: held by {seal.holder!r}"
            )
        seq = self._next_seq()
        record = Reweigh(reweigh_id, seal_id, branch, lo, hi, seq)
        self.reweighs.append(record)
        self._reweigh_ids.add(reweigh_id)
        return record

    # ------------------------------------------------------------- return
    def return_seal(self, seal_id: str, branch: str) -> Seal:
        """A branch hands a seal back to HQ at the end of the trip."""

        seal = self._get_live(seal_id)
        if seal.holder != branch:
            raise LineageError(
                f"branch {branch!r} cannot return {seal_id!r}: held by {seal.holder!r}"
            )
        seal.surrendered = True
        seal.returned_to_hq = True
        seal.holder = "HQ"
        return seal

    # ------------------------------------------------------------- traversal
    def children_of(self, seal_id: str) -> list[str]:
        """Child seals born from ``seal_id`` in a split/merge handover.

        A whole-seal forward carries the same seal onward and is deliberately
        not a parent/child edge: the lineage tree follows physical splits and
        merges, not location changes.
        """

        out: list[str] = []
        for batch in self.batches:
            if batch.kind != "split_merge":
                continue
            if seal_id in batch.old_seal_ids:
                out.extend(s["seal_id"] for s in batch.new_seals)
        return out

    def parent_of(self, seal_id: str) -> str | None:
        for batch in self.batches:
            if any(s["seal_id"] == seal_id for s in batch.new_seals):
                if batch.kind == "forward":
                    return batch.old_seal_ids[0]
                # split/merge: return the single parent when unambiguous
                if len(batch.old_seal_ids) == 1:
                    return batch.old_seal_ids[0]
        return None

    def roots(self) -> list[Seal]:
        root_ids = sorted(self.outbound)
        return [self.seals[sid] for sid in root_ids if sid in self.seals]

    def live_seals(self) -> list[Seal]:
        return [s for s in self.seals.values() if not s.surrendered]

    def coin_trace(self, coin_id: str) -> list[dict[str, Any]]:
        """Per-coin itinerary across the whole trip."""

        itinerary: list[dict[str, Any]] = []
        for batch in self.batches:
            for raw in batch.new_seals:
                ids = self._all_ids(raw.get("coin_ids", {}))
                if coin_id in ids:
                    itinerary.append(
                        {
                            "seq": batch.seq,
                            "batch_id": batch.batch_id,
                            "kind": batch.kind,
                            "seal_id": raw["seal_id"],
                            "holder": raw["holder"],
                        }
                    )
        return itinerary

    # ------------------------------------------------------------- reconcile
    def reconcile(
        self,
        mass_domains: Mapping[str, Sequence[int]],
        scale_receipts: Sequence[Mapping[str, Any]],
        *,
        proposed_masses: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Whole-trip HQ write-off check.

        ``scale_receipts`` are the original weighing-room receipts (each with
        ``counts`` and an interval).  Reweigh receipts recorded along the way
        are pulled from this ledger.  Every interval -- original appraisal and
        every branch reweigh -- must share one period-mass interpretation.
        """

        problems: list[dict[str, str]] = []

        # 1. Every outbound seal must belong to a closed lineage tree.
        returned = {sid for sid, seal in self.seals.items() if seal.returned_to_hq}
        in_transit: list[str] = []
        for root in self.roots():
            frontier = [root.seal_id]
            seen: set[str] = set()
            closed_branch: list[str] = []
            while frontier:
                sid = frontier.pop()
                if sid in seen:
                    continue
                seen.add(sid)
                seal = self.seals[sid]
                children = self.children_of(sid)
                if not children:
                    if seal.returned_to_hq:
                        closed_branch.append(sid)
                    else:
                        in_transit.append(sid)
                else:
                    if seal.returned_to_hq:
                        problems.append(
                            {
                                "code": "duplicate_writeoff",
                                "seal_id": sid,
                                "message": (
                                    f"seal {sid!r} has child seals but was itself written off; "
                                    "the same coins would be booked twice"
                                ),
                            }
                        )
                    frontier.extend(children)
            if not closed_branch and not in_transit:
                problems.append(
                    {
                        "code": "missing_branch",
                        "seal_id": root.seal_id,
                        "message": f"root seal {root.seal_id!r} has no returned terminal seal",
                    }
                )

        # 2. Double surrender / duplicate write-off across the ledger.
        surrendered_ids = [sid for sid, s in self.seals.items() if s.surrendered]
        returned_terminal = sorted(returned)
        coin_final: dict[str, str] = {}
        for sid in returned_terminal:
            seal = self.seals[sid]
            for cid in self._all_ids(seal.coin_ids):
                if cid in coin_final:
                    problems.append(
                        {
                            "code": "double_spend",
                            "coin_id": cid,
                            "message": (
                                f"coin {cid!r} is written off in both {coin_final[cid]!r} "
                                f"and {sid!r}"
                            ),
                        }
                    )
                coin_final[cid] = sid

        # 3. Outbound conservation: every outbound coin is accounted exactly once.
        outbound_coins = {
            cid
            for root in self.roots()
            for cid in self._all_ids(root.coin_ids)
        }
        final_coins = set(coin_final)
        for cid in sorted(outbound_coins - final_coins):
            problems.append(
                {
                    "code": "missing_in_transit",
                    "coin_id": cid,
                    "message": f"outbound coin {cid!r} never reaches a returned terminal seal",
                }
            )
        for cid in sorted(final_coins - outbound_coins):
            problems.append(
                {
                    "code": "unauthorized_coin",
                    "coin_id": cid,
                    "message": f"returned coin {cid!r} is not on the outbound ledger (possible fraud)",
                }
            )

        # 4. One shared weight interpretation for the entire trip.
        csp = WeightCSP()
        for period, masses in mass_domains.items():
            csp.declare_period(period, masses)
        receipt_index: list[dict[str, Any]] = []
        for receipt in scale_receipts:
            csp.add_constraint(
                receipt["receipt_id"],
                receipt["counts"],
                int(receipt["lo"]),
                int(receipt["hi"]),
            )
            receipt_index.append(
                {"source": "scale_office", "receipt_id": receipt["receipt_id"]}
            )
        for record in self.reweighs:
            seal = self.seals[record.seal_id]
            csp.add_constraint(
                record.reweigh_id, seal.contents, record.lo, record.hi
            )
            receipt_index.append(
                {"source": "branch_reweigh", "receipt_id": record.reweigh_id,
                 "seal_id": record.seal_id, "branch": record.branch}
            )

        weight_report: dict[str, Any]
        if proposed_masses is not None:
            try:
                broken = csp.check_fixed(dict(proposed_masses))
            except WeightError as exc:
                broken = []
                problems.append(
                    {"code": "bad_mass_table", "message": str(exc)}
                )
            weight_report = {
                "satisfiable": not broken,
                "masses": dict(sorted(proposed_masses.items())),
                "conflicting_receipts": [c.receipt_id for c in broken],
            }
        else:
            weight_report = csp.query()
        if not weight_report["satisfiable"]:
            problems.append(
                {
                    "code": "weight_conflict",
                    "message": "no single mass interpretation supports every receipt",
                    "conflicting_receipts": weight_report["conflicting_receipts"],
                }
            )

        # 5. Per-coin itinerary for everything actually written off.
        traces = {
            cid: self.coin_trace(cid)
            for cid in sorted(final_coins)
        }
        seal_report = {
            sid: {
                "holder": seal.holder,
                "contents": dict(seal.contents),
                "returned_to_hq": seal.returned_to_hq,
                "parent_batch": seal.parent_batch,
            }
            for sid, seal in sorted(self.seals.items())
        }
        return {
            "ok": not problems,
            "problems": problems,
            "in_transit_seals": sorted(set(in_transit)),
            "returned_seals": returned_terminal,
            "weight_report": weight_report,
            "receipt_index": receipt_index,
            "seals": seal_report,
            "coin_traces": traces,
            "outbound": {sid: dict(c) for sid, c in sorted(self.outbound.items())},
        }

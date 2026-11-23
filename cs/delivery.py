"""Two delivery models with a durable, auditable public record.

Both models answer the same question: *which original cells has the public
actually seen, and how did a shipped label get there?*  The public record is
append-only: once a cell has been shown to an audience it cannot be un-seen, so
withdrawing a label never removes cells.  In-library misprints and retained
samples are recorded but **not** public.

Central model (集中制版)
    The archive bakes the crop *and* the signature into a final master
    (终版), then ships it.  A **delivery receipt** (送达回执) records what was
    handed to a venue; a **retained sample** (留样) records a copy kept in the
    archive.  Only dispatched, publicly-shown masters extend the public scope.

Branch model (分馆制版)
    Each branch receives a **plate** (制版片) that still carries original-cell
    identity, plus a redeemable **print voucher** (出印凭条).  The branch lays
    out the signature itself and must *redeem* (兑销) the voucher before it
    releases anything to the public.  Redemption is checked against the whole
    set of vouchers confirmed for shipment, so two branches that each hold a
    partial licence cannot jointly complete a forbidden pattern: an
    undetermined voucher is never treated as a positive release by another
    branch.

Every recorded shipment includes the composition, the crop (original cell
identity), the signature cells, and the cumulative public-check verdict, so a
shipped label can always be traced back through crop, signature and the
accumulated public record.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .exhibit import (
    Cell,
    Composition,
    CompositionCheck,
    Panel,
    check_composition,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    label_id   TEXT,
    originals  TEXT NOT NULL,
    details    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS public_cells (
    original TEXT NOT NULL,
    row      INTEGER NOT NULL,
    col      INTEGER NOT NULL,
    label_id TEXT,
    PRIMARY KEY (original, row, col)
);
CREATE TABLE IF NOT EXISTS vouchers (
    voucher_id TEXT PRIMARY KEY,
    label_id   TEXT NOT NULL,
    branch     TEXT NOT NULL,
    original   TEXT NOT NULL,
    cells      TEXT NOT NULL,
    status     TEXT NOT NULL
);
"""


class DeliveryError(Exception):
    """Rejected delivery / redemption."""


# --------------------------------------------------------------- public record


@dataclass
class PublicRecord:
    """Append-only set of original cells the public has already seen."""

    cells: dict[str, set[Cell]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def public_cells(self) -> dict[str, set[Cell]]:
        return {orig: set(cells) for orig, cells in self.cells.items()}

    def is_public(self, original: str, cell: Cell) -> bool:
        return cell in self.cells.get(original, set())

    def _append(self, kind: str, label_id: str | None, originals: Iterable[str],
                details: dict[str, Any]) -> None:
        self.events.append(
            {"kind": kind, "label_id": label_id,
             "originals": sorted(set(originals)), "details": details}
        )

    def show(
        self,
        *,
        label_id: str,
        composition: Composition,
        check: CompositionCheck,
        trace: dict[str, Any] | None = None,
        cells_by_original: dict[str, set[Cell]] | None = None,
    ) -> None:
        """Record a label actually shown to the public (irreversible).

        ``cells_by_original`` defaults to the composition's crop; callers that
        only know the concrete cells (a redeemed branch voucher) can pass them
        explicitly so the record stores exactly what was printed.
        """

        if not check.feasible:
            raise DeliveryError(f"label {label_id} is not feasible; cannot show")
        cells_by_original = cells_by_original or composition.crop_cells_by_original()
        added: dict[str, list[Cell]] = {}
        for orig, cells in cells_by_original.items():
            current = self.cells.setdefault(orig, set())
            new = cells - current
            if new:
                added[orig] = sorted(new)
            current |= cells
        self._append(
            "shown",
            label_id,
            cells_by_original.keys(),
            {
                "composition": composition.id,
                "crop": {o: sorted(c) for o, c in cells_by_original.items()},
                "added_public": {o: [list(c) for c in cs] for o, cs in added.items()},
                "signature": check.signature.as_dict() if check.signature else None,
                "trace": trace or {},
            },
        )

    def withdraw(self, *, label_id: str) -> None:
        """A withdrawn label is no longer shown, but seen cells stay public."""

        self._append("withdrawn", label_id, [], {"note": "public cells are retained"})

    def record_nonpublic(
        self, *, label_id: str, kind: str, detail: str
    ) -> None:
        """Record a misprint/retained sample; does not extend public scope."""

        if kind not in {"misprint", "retained"}:
            raise DeliveryError(f"unknown non-public record kind {kind!r}")
        self._append(kind, label_id, [], {"detail": detail, "public": False})

    def audit(self, label_id: str) -> dict[str, Any]:
        events = [e for e in self.events if e["label_id"] == label_id]
        cells = {
            orig: sorted(c for c in cs)
            for orig, cs in self.cells.items()
        }
        return {"label_id": label_id, "events": events, "public_cells": cells}


def _completes(
    public: set[Cell], addition: set[Cell], protections: Iterable[Iterable[Cell]]
) -> tuple[Cell, ...] | None:
    merged = public | addition
    for group in protections:
        gs = set(group)
        if gs and gs <= merged:
            return tuple(sorted(gs))
    return None


# ------------------------------------------------------------ central masters


@dataclass
class BakedMaster:
    """A central final version: crop + signature already burned in."""

    label_id: str
    composition: Composition
    check: CompositionCheck
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "label_id": self.label_id,
            "composition": self.composition.id,
            "check": self.check.as_dict(),
            "digest": self.digest,
        }


def bake_master(
    *,
    label_id: str,
    composition: Composition,
    panel: Panel,
    reserved: Iterable[Cell],
    signature_lengths: Sequence[int],
    license_region: set[Cell],
    record: PublicRecord,
    protections: dict[str, list[Iterable[Cell]]],
    original_ids: Iterable[str],
) -> BakedMaster:
    check = check_composition(
        composition,
        panel=panel,
        reserved=reserved,
        signature_lengths=signature_lengths,
        license_region=license_region,
        public_cells=record.public_cells(),
        protections=protections,
        original_ids=original_ids,
    )
    if not check.feasible:
        raise DeliveryError(
            f"cannot bake {label_id}: " + "; ".join(check.reasons)
        )
    payload = {
        "label_id": label_id,
        "composition": composition.id,
        "crop": {o: sorted(c) for o, c in composition.crop_cells_by_original().items()},
        "signature": check.signature.as_dict() if check.signature else None,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return BakedMaster(label_id, composition, check, digest)


def dispatch_master(
    master: BakedMaster, *, receipt: str, record: PublicRecord
) -> None:
    """Ship a baked master to a venue; the public scope grows now."""

    record.show(
        label_id=master.label_id,
        composition=master.composition,
        check=master.check,
        trace={"model": "central", "receipt": receipt, "digest": master.digest},
    )


def retain_master(master: BakedMaster, *, record: PublicRecord, note: str) -> None:
    """Keep an archive copy: recorded, but never public."""

    record.record_nonpublic(
        label_id=master.label_id, kind="retained", detail=f"{note} (digest {master.digest})"
    )


# --------------------------------------------------------------- branch model


@dataclass
class Voucher:
    voucher_id: str
    label_id: str
    branch: str
    original: str
    cells: set[Cell]
    status: str = "issued"  # issued | redeemed | cancelled

    def as_dict(self) -> dict[str, Any]:
        return {
            "voucher": self.voucher_id,
            "label": self.label_id,
            "branch": self.branch,
            "original": self.original,
            "cells": sorted([r, c] for r, c in self.cells),
            "status": self.status,
        }


@dataclass
class VoucherBook:
    vouchers: dict[str, Voucher] = field(default_factory=dict)

    def issue(
        self, *, voucher_id: str, label_id: str, branch: str,
        original: str, cells: Iterable[Cell],
    ) -> Voucher:
        if voucher_id in self.vouchers:
            raise DeliveryError(f"voucher {voucher_id} already issued")
        voucher = Voucher(
            voucher_id=voucher_id, label_id=label_id, branch=branch,
            original=original, cells=set(cells),
        )
        self.vouchers[voucher_id] = voucher
        return voucher

    def cancel(self, voucher_id: str) -> Voucher:
        voucher = self.vouchers[voucher_id]
        if voucher.status != "issued":
            raise DeliveryError(f"voucher {voucher_id} is {voucher.status}, not issued")
        voucher.status = "cancelled"
        return voucher

    def pending(self) -> list[Voucher]:
        return [v for v in self.vouchers.values() if v.status == "issued"]


@dataclass
class RedemptionResult:
    redeemed: list[Voucher]
    public_after: dict[str, set[Cell]]
    checks: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "redeemed": [v.as_dict() for v in self.redeemed],
            "public_after": {o: sorted(c) for o, c in self.public_after.items()},
            "checks": self.checks,
        }


def redeem_batch(
    book: VoucherBook,
    voucher_ids: Sequence[str],
    *,
    record: PublicRecord,
    protections: dict[str, list[Iterable[Cell]]],
    compositions: dict[str, Composition] | None = None,
    checks: dict[str, CompositionCheck] | None = None,
) -> RedemptionResult:
    """Redeem a set of vouchers atomically, refusing any joint completion.

    Two branches may each hold a legal partial slice; redeeming them *together*
    must still not complete a forbidden group.  The whole batch is rejected if
    the combined post-redemption public set would complete any protection group.
    Only ``issued`` vouchers redeem; a cancelled voucher cannot be revived and an
    unredeemed voucher is never treated as a definite release.
    """

    chosen: list[Voucher] = []
    for vid in voucher_ids:
        voucher = book.vouchers.get(vid)
        if voucher is None:
            raise DeliveryError(f"unknown voucher {vid}")
        if voucher.status != "issued":
            raise DeliveryError(f"voucher {vid} is {voucher.status}, not redeemable")
        chosen.append(voucher)

    public = record.public_cells()
    additions: dict[str, set[Cell]] = {}
    for voucher in chosen:
        additions.setdefault(voucher.original, set()).update(voucher.cells)

    for orig, cells in additions.items():
        done = _completes(public.get(orig, set()), cells, protections.get(orig, []))
        if done is not None:
            raise DeliveryError(
                f"joint redemption completes a protection group on {orig}: {list(done)}"
            )

    checks_out: list[dict[str, Any]] = []
    for voucher in chosen:
        voucher.status = "redeemed"
        record.show(
            label_id=voucher.label_id,
            composition=(compositions or {}).get(voucher.label_id)
            or _voucher_composition(voucher),
            check=(checks or {}).get(voucher.label_id)
            or CompositionCheck(
                composition_id=f"voucher:{voucher.voucher_id}",
                feasible=True,
                printed=set(voucher.cells),
                originals={voucher.original},
            ),
            cells_by_original={voucher.original: set(voucher.cells)},
            trace={
                "model": "branch",
                "voucher": voucher.voucher_id,
                "branch": voucher.branch,
                "redeemed": True,
            },
        )
        checks_out.append(
            {
                "voucher": voucher.voucher_id,
                "label": voucher.label_id,
                "branch": voucher.branch,
                "cells": sorted([r, c] for r, c in voucher.cells),
            }
        )

    return RedemptionResult(
        redeemed=chosen,
        public_after=record.public_cells(),
        checks=checks_out,
    )


def _voucher_composition(voucher: Voucher) -> Composition:
    # A minimal composition witness when the caller does not supply the real
    # layout: one block covering exactly the voucher's cells.
    from .exhibit import Block

    rows = [r for r, _ in voucher.cells]
    cols = [c for _, c in voucher.cells]
    rect = (min(rows), min(cols), max(rows), max(cols))
    return Composition(
        id=f"voucher:{voucher.voucher_id}",
        blocks=(Block(voucher.original, rect, False, (0, 0)),),
    )


# ------------------------------------------------------- persistent record


class DeliveryStore:
    """SQLite-backed public record + voucher book, for an offline demo."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.book = VoucherBook()

    @classmethod
    def open(cls, path: str) -> "DeliveryStore":
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        store = cls(conn)
        store._load()
        return store

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DeliveryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _load(self) -> None:
        self.record = PublicRecord()
        for row in self.conn.execute("SELECT original, row, col FROM public_cells"):
            self.record.cells.setdefault(row["original"], set()).add(
                (int(row["row"]), int(row["col"]))
            )
        for row in self.conn.execute(
            "SELECT kind, label_id, originals, details FROM public_events ORDER BY seq"
        ):
            self.record.events.append(
                {
                    "kind": row["kind"],
                    "label_id": row["label_id"],
                    "originals": json.loads(row["originals"]),
                    "details": json.loads(row["details"]),
                }
            )
        for row in self.conn.execute("SELECT * FROM vouchers"):
            self.book.vouchers[row["voucher_id"]] = Voucher(
                voucher_id=row["voucher_id"],
                label_id=row["label_id"],
                branch=row["branch"],
                original=row["original"],
                cells={tuple(c) for c in json.loads(row["cells"])},
                status=row["status"],
            )

    def _persist(self) -> None:
        self.conn.execute("DELETE FROM public_cells")
        for orig, cells in self.record.cells.items():
            for r, c in cells:
                self.conn.execute(
                    "INSERT OR IGNORE INTO public_cells(original,row,col,label_id)"
                    " VALUES(?,?,?,NULL)",
                    (orig, r, c),
                )
        self.conn.execute("DELETE FROM public_events")
        for event in self.record.events:
            self.conn.execute(
                "INSERT INTO public_events(kind,label_id,originals,details)"
                " VALUES(?,?,?,?)",
                (
                    event["kind"],
                    event["label_id"],
                    json.dumps(event["originals"]),
                    json.dumps(event["details"], ensure_ascii=False, sort_keys=True),
                ),
            )
        self.conn.execute("DELETE FROM vouchers")
        for voucher in self.book.vouchers.values():
            self.conn.execute(
                "INSERT INTO vouchers(voucher_id,label_id,branch,original,cells,status)"
                " VALUES(?,?,?,?,?,?)",
                (
                    voucher.voucher_id,
                    voucher.label_id,
                    voucher.branch,
                    voucher.original,
                    json.dumps(sorted([list(c) for c in voucher.cells])),
                    voucher.status,
                ),
            )

    def show(self, **kwargs: Any) -> None:
        self.record.show(**kwargs)
        self._persist()

    def withdraw(self, **kwargs: Any) -> None:
        self.record.withdraw(**kwargs)
        self._persist()

    def record_nonpublic(self, **kwargs: Any) -> None:
        self.record.record_nonpublic(**kwargs)
        self._persist()

    def issue(self, **kwargs: Any) -> Voucher:
        voucher = self.book.issue(**kwargs)
        self._persist()
        return voucher

    def cancel(self, voucher_id: str) -> Voucher:
        voucher = self.book.cancel(voucher_id)
        self._persist()
        return voucher

    def redeem_batch(self, voucher_ids: Sequence[str], **kwargs: Any) -> RedemptionResult:
        try:
            result = redeem_batch(self.book, voucher_ids, record=self.record, **kwargs)
        except DeliveryError:
            self._persist()
            raise
        self._persist()
        return result

    def audit(self, label_id: str) -> dict[str, Any]:
        return self.record.audit(label_id)

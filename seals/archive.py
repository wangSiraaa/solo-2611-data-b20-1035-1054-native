"""Archive service: mold registry, imprint versions and origin reconstruction.

The archive stores three independent fact families:

* pressed bottom molds (one per furnace batch, reusable for many boxes),
* the seal clay spent issuing labels (owned by the reagent service),
* the imprint strips actually read on boxes.

It never pre-fills which furnace batch a box contains. A box's possible
batches are derived, on every query, by running the matching engine over the
current archive. Old reads that gain new cells must be filed as a new
version; the old version stays on record.
"""

from __future__ import annotations

import json
import sqlite3

from reagent.errors import Conflict, NotFoundError, ValidationError
from reagent.rules import advance_day, require_day, require_int, require_text
from reagent.schema import transaction

from . import matching
from .matching import (
    HOLE_MISSING,
    HOLE_PRESENT,
    Imprint,
    Mold,
    Voucher,
    possible_batches,
    validate_imprint,
)

MAX_TEETH = 40
MAX_IMPRINT_CELLS = 20


class ArchiveService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- molds --------------------------------------------------------------
    def register_mold(
        self,
        mold_id: str,
        furnace_batch: str,
        pattern: str,
        day: int,
        splice_adjacencies: list[int] | None = None,
    ) -> dict:
        require_text(mold_id, "mold_id")
        require_text(furnace_batch, "furnace_batch")
        require_text(pattern, "pattern")
        require_day(day)
        if not pattern or any(c not in "01" for c in pattern):
            raise ValidationError("mold pattern must be a non-empty string of 0/1")
        if len(pattern) > MAX_TEETH:
            raise ValidationError(f"a mold has at most {MAX_TEETH} teeth")
        marks = sorted(set(splice_adjacencies or []))
        for mark in marks:
            require_int(mark, "splice adjacency", minimum=0)
            if mark >= len(pattern) - 1:
                raise ValidationError(
                    f"mold {mold_id}: adjacency {mark} does not exist for "
                    f"{len(pattern)} teeth"
                )
        with transaction(self.conn):
            advance_day(self.conn, day)
            if self.conn.execute(
                "SELECT 1 FROM molds WHERE mold_id = ?", (mold_id,)
            ).fetchone():
                raise Conflict(f"mold {mold_id!r} already registered")
            if self.conn.execute(
                "SELECT 1 FROM molds WHERE furnace_batch = ?", (furnace_batch,)
            ).fetchone():
                raise Conflict(
                    f"furnace batch {furnace_batch!r} already has a registered mold"
                )
            self.conn.execute(
                "INSERT INTO molds(mold_id, furnace_batch, pattern, tooth_count, "
                "registered_day) VALUES(?, ?, ?, ?, ?)",
                (mold_id, furnace_batch, pattern, len(pattern), day),
            )
            for mark in marks:
                self.conn.execute(
                    "INSERT INTO mold_splice_marks(mold_id, adjacency) VALUES(?, ?)",
                    (mold_id, mark),
                )
            return self.get_mold(mold_id)

    def register_voucher(
        self,
        voucher_id: str,
        mold_id: str,
        shift_id: str,
        first_tooth: int,
        last_tooth: int,
        min_skip: int,
        max_skip: int,
    ) -> dict:
        require_text(voucher_id, "voucher_id")
        require_text(mold_id, "mold_id")
        require_text(shift_id, "shift_id")
        first_tooth = require_int(first_tooth, "first_tooth", minimum=0)
        last_tooth = require_int(last_tooth, "last_tooth", minimum=0)
        min_skip = require_int(min_skip, "min_skip", minimum=1)
        max_skip = require_int(max_skip, "max_skip", minimum=1)
        if last_tooth < first_tooth or max_skip < min_skip:
            raise ValidationError("voucher intervals must be ordered")
        with transaction(self.conn):
            mold = self._require_mold(mold_id)
            if last_tooth >= mold["tooth_count"] or first_tooth < 0:
                raise ValidationError("voucher teeth lie outside the mold")
            if max_skip > last_tooth - first_tooth + 1:
                raise ValidationError(
                    "voucher max_skip exceeds its own tooth span"
                )
            if self.conn.execute(
                "SELECT 1 FROM waste_vouchers WHERE voucher_id = ?", (voucher_id,)
            ).fetchone():
                raise Conflict(f"voucher {voucher_id!r} already registered")
            self.conn.execute(
                "INSERT INTO waste_vouchers(voucher_id, mold_id, shift_id, "
                "first_tooth, last_tooth, min_skip, max_skip) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (voucher_id, mold_id, shift_id, first_tooth, last_tooth,
                 min_skip, max_skip),
            )
            return self.get_voucher(voucher_id)

    def get_mold(self, mold_id: str) -> dict:
        row = self._require_mold(mold_id)
        marks = [
            int(r["adjacency"])
            for r in self.conn.execute(
                "SELECT adjacency FROM mold_splice_marks WHERE mold_id = ? "
                "ORDER BY adjacency",
                (mold_id,),
            )
        ]
        return {
            "mold_id": row["mold_id"],
            "furnace_batch": row["furnace_batch"],
            "pattern": row["pattern"],
            "tooth_count": int(row["tooth_count"]),
            "splice_adjacencies": marks,
            "registered_day": row["registered_day"],
        }

    def get_voucher(self, voucher_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM waste_vouchers WHERE voucher_id = ?", (voucher_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown voucher {voucher_id!r}")
        return {k: row[k] for k in row.keys()}

    # -- imprint reads (versioned) -----------------------------------------
    def record_imprint(
        self,
        imprint_id: str,
        label_id: str,
        segments: list[str],
        day: int,
        splice_marks: list[int] | None = None,
        locating_hole: str = HOLE_PRESENT,
        note: str | None = None,
    ) -> dict:
        require_text(imprint_id, "imprint_id")
        require_text(label_id, "label_id")
        require_day(day)
        if locating_hole not in (HOLE_PRESENT, HOLE_MISSING):
            raise ValidationError("locating_hole must be 'present' or 'missing'")
        imprint = Imprint(
            tuple(segments),
            frozenset(splice_marks or []),
            locating_hole,
        )
        validate_imprint(imprint, max_cells=MAX_IMPRINT_CELLS)
        with transaction(self.conn):
            advance_day(self.conn, day)
            label = self.conn.execute(
                "SELECT * FROM labels WHERE label_id = ?", (label_id,)
            ).fetchone()
            if label is None:
                raise NotFoundError(
                    f"label {label_id!r} has no legal seal issuance; its imprint "
                    "cannot be archived"
                )
            versions = self.conn.execute(
                "SELECT MAX(version) AS v FROM imprint_versions WHERE imprint_id = ?",
                (imprint_id,),
            ).fetchone()["v"]
            version = 1 if versions is None else int(versions) + 1
            existing_labels = self.conn.execute(
                "SELECT DISTINCT label_id FROM imprint_versions WHERE imprint_id = ?",
                (imprint_id,),
            ).fetchall()
            if existing_labels and existing_labels[0]["label_id"] != label_id:
                raise Conflict(
                    f"imprint {imprint_id!r} already belongs to another label"
                )
            self.conn.execute(
                "INSERT INTO imprint_versions(imprint_id, version, label_id, "
                "segments_json, splice_marks_json, locating_hole, recorded_day, note) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    imprint_id,
                    version,
                    label_id,
                    json.dumps(list(segments)),
                    json.dumps(sorted(splice_marks or [])),
                    locating_hole,
                    day,
                    note,
                ),
            )
            self.conn.execute(
                "UPDATE labels SET current_version = ? WHERE label_id = ?",
                (version, label_id),
            )
            return self.get_imprint(imprint_id, version)

    def get_imprint(self, imprint_id: str, version: int | None = None) -> dict:
        if version is None:
            row = self.conn.execute(
                "SELECT * FROM imprint_versions WHERE imprint_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (imprint_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM imprint_versions WHERE imprint_id = ? AND version = ?",
                (imprint_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown imprint {imprint_id!r} version {version}")
        return {
            "imprint_id": row["imprint_id"],
            "version": row["version"],
            "label_id": row["label_id"],
            "segments": json.loads(row["segments_json"]),
            "splice_marks": json.loads(row["splice_marks_json"]),
            "locating_hole": row["locating_hole"],
            "recorded_day": row["recorded_day"],
            "note": row["note"],
        }

    def list_imprints(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT imprint_id, MAX(version) AS v FROM imprint_versions "
            "GROUP BY imprint_id ORDER BY imprint_id"
        ).fetchall()
        return [self.get_imprint(r["imprint_id"], r["v"]) for r in rows]

    # -- reconstruction ------------------------------------------------------
    def _load_molds(self) -> tuple[Mold, ...]:
        out: list[Mold] = []
        for row in self.conn.execute("SELECT * FROM molds ORDER BY mold_id"):
            marks = frozenset(
                int(r["adjacency"])
                for r in self.conn.execute(
                    "SELECT adjacency FROM mold_splice_marks WHERE mold_id = ?",
                    (row["mold_id"],),
                )
            )
            out.append(
                Mold(
                    row["mold_id"],
                    row["furnace_batch"],
                    row["pattern"],
                    marks,
                )
            )
        return tuple(out)

    def _load_vouchers(self) -> tuple[Voucher, ...]:
        return tuple(
            Voucher(
                r["voucher_id"],
                r["mold_id"],
                r["shift_id"],
                int(r["first_tooth"]),
                int(r["last_tooth"]),
                int(r["min_skip"]),
                int(r["max_skip"]),
            )
            for r in self.conn.execute(
                "SELECT * FROM waste_vouchers ORDER BY voucher_id"
            )
        )

    def _imprint_from_row(self, row: sqlite3.Row) -> Imprint:
        return Imprint(
            tuple(json.loads(row["segments_json"])),
            frozenset(json.loads(row["splice_marks_json"])),
            row["locating_hole"],
        )

    def reconstruct_imprint(
        self, imprint_id: str, version: int | None = None
    ) -> dict:
        row = self._require_imprint_row(imprint_id, version)
        imprint = self._imprint_from_row(row)
        solutions, failures = matching.reconstruct(
            imprint, self._load_molds(), self._load_vouchers()
        )
        payload = {
            "imprint_id": imprint_id,
            "version": row["version"],
            "label_id": row["label_id"],
            "segments": list(imprint.segments),
            "splice_marks": sorted(imprint.splice_marks),
            "locating_hole": imprint.locating_hole,
            "possible_batches": possible_batches(solutions),
            "solutions": [s.as_dict() for s in solutions],
            "failures": [f.as_dict() for f in failures],
            "resolvable": bool(solutions),
        }
        self._persist_reconstructions(row, solutions)
        return payload

    def reconstruct_label(self, label_id: str) -> dict:
        """Independently query every possible batch / segment for one label."""
        rows = self.conn.execute(
            "SELECT * FROM imprint_versions WHERE label_id = ? ORDER BY version",
            (label_id,),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"label {label_id!r} has no archived imprint")
        return {
            "label_id": label_id,
            "versions": [
                self.reconstruct_imprint(r["imprint_id"], r["version"])
                for r in rows
            ],
        }

    def _persist_reconstructions(
        self, row: sqlite3.Row, solutions: list[matching.Solution]
    ) -> None:
        self.conn.execute(
            "DELETE FROM imprint_reconstructions WHERE imprint_id = ? AND version = ?",
            (row["imprint_id"], row["version"]),
        )
        for solution in solutions:
            self.conn.execute(
                "INSERT INTO imprint_reconstructions(imprint_id, version, label_id, "
                "mold_id, direction, placement_json, vouchers_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    row["imprint_id"],
                    row["version"],
                    row["label_id"],
                    solution.mold_id,
                    solution.direction,
                    json.dumps(solution.as_dict()["segments"], sort_keys=True),
                    json.dumps(solution.as_dict()["gaps"], sort_keys=True),
                ),
            )

    # -- helpers ------------------------------------------------------------
    def _require_mold(self, mold_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM molds WHERE mold_id = ?", (mold_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown mold {mold_id!r}")
        return row

    def _require_imprint_row(
        self, imprint_id: str, version: int | None
    ) -> sqlite3.Row:
        if version is None:
            row = self.conn.execute(
                "SELECT * FROM imprint_versions WHERE imprint_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (imprint_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM imprint_versions WHERE imprint_id = ? AND version = ?",
                (imprint_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown imprint {imprint_id!r} version {version}")
        return row

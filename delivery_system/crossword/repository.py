"""Translator-side coinage repository.

A coinage sheet (造词单) is bound to the *actual translated block* it was cut
for, identified by ``(package_id, block_id)``.  The block must be a real chunk
of a frozen package, and the clues a sheet points at must actually be covered
by that block — same segment id *and* the same source revision that the package
froze.  Two packages that print identical body text but number their blocks
differently therefore cannot share a sheet: reusing one is rejected.

Sheets are stored immutably; a lexicographer withdraws a coinage (拼法) by
recording a tombstone, which affects *new* answer books but never an
already-issued proof (已送印的谜盘保留原稿源).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Protocol

from .errors import (
    BannedCombination,
    CoinageCoverageError,
    SheetBlockMismatch,
    SheetImmutable,
)
from .answerbook import resolve_sheet_entries
from .models import (
    Ban,
    ClueSense,
    CoinageEntry,
    CoinageSheet,
    Root,
    SuffixClass,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    sheet_id      TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    language      TEXT NOT NULL,
    package_id    TEXT NOT NULL,
    block_id      TEXT NOT NULL,
    content_sha   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    sheet_id      TEXT NOT NULL REFERENCES sheets(sheet_id) ON DELETE CASCADE,
    entry_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    clue_id       TEXT NOT NULL,
    op            TEXT NOT NULL,
    root_id       TEXT,
    literal       TEXT NOT NULL,
    suffix        TEXT NOT NULL,
    PRIMARY KEY (sheet_id, entry_id)
);
CREATE TABLE IF NOT EXISTS sheet_bans (
    sheet_id      TEXT NOT NULL REFERENCES sheets(sheet_id) ON DELETE CASCADE,
    ban_id        TEXT NOT NULL,
    clue_id       TEXT NOT NULL,
    root_id       TEXT NOT NULL,
    suffix        TEXT NOT NULL,
    PRIMARY KEY (sheet_id, ban_id)
);
CREATE TABLE IF NOT EXISTS withdrawals (
    sheet_id      TEXT NOT NULL,
    entry_id      TEXT NOT NULL,
    withdrawn_at  TEXT NOT NULL,
    reason        TEXT NOT NULL,
    PRIMARY KEY (sheet_id, entry_id)
);
CREATE TABLE IF NOT EXISTS issued_proofs (
    proof_id      TEXT PRIMARY KEY,
    package_id    TEXT NOT NULL,
    block_id      TEXT NOT NULL,
    content_json  TEXT NOT NULL,
    content_sha   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


class PackageReader(Protocol):
    def get_package(self, package_id: str) -> dict[str, object]: ...


class ClueReader(Protocol):
    def get_clue(self, clue_id: str) -> ClueSense: ...

    def get_root(self, root_id: str) -> Root: ...

    def get_suffix_class(self, class_id: str) -> SuffixClass: ...

    def is_banned(self, clue_id: str, root_id: str, suffix: str) -> bool: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(os.fspath(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _sheet_content_sha(
    document_id: str,
    language: str,
    package_id: str,
    block_id: str,
    entries: Iterable[CoinageEntry],
    bans: Iterable[Ban],
) -> str:
    payload = {
        "document_id": document_id,
        "language": language,
        "package_id": package_id,
        "block_id": block_id,
        "entries": [
            {
                "entry_id": e.entry_id,
                "clue_id": e.clue_id,
                "op": e.op,
                "root_id": e.root_id,
                "literal": e.literal,
                "suffix": e.suffix,
            }
            for e in entries
        ],
        "bans": [
            {
                "ban_id": b.ban_id,
                "clue_id": b.clue_id,
                "root_id": b.root_id,
                "suffix": b.suffix,
            }
            for b in bans
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class CoinageRepository:
    def __init__(
        self,
        db_path: str,
        packages: PackageReader,
        clues: ClueReader,
    ) -> None:
        self.db_path = os.fspath(db_path)
        self.packages = packages
        self.clues = clues
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = _connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _txn(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # block coverage
    # ------------------------------------------------------------------
    def _block(self, package_id: str, block_id: str) -> tuple[dict[str, object], dict[str, int]]:
        package = self.packages.get_package(package_id)
        snapshot = {
            str(item["segment_id"]): int(item["revision"])
            for item in package.get("source_snapshot", [])  # type: ignore[union-attr]
        }
        chunk = None
        for candidate in package.get("chunks", []):  # type: ignore[union-attr]
            if candidate.get("translation_id") == block_id:  # type: ignore[union-attr]
                chunk = candidate
                break
        if chunk is None:
            raise SheetBlockMismatch(
                f"block {block_id!r} is not a chunk of package {package_id!r}"
            )
        return chunk, snapshot

    def _validate_entries(
        self,
        document_id: str,
        package_id: str,
        block_id: str,
        entries: Iterable[CoinageEntry],
        sheet_bans: Iterable[Ban],
    ) -> None:
        chunk, snapshot = self._block(package_id, block_id)
        source_ids = {str(x) for x in chunk.get("source_ids", [])}  # type: ignore[union-attr]
        seen_entries: set[str] = set()
        ban_set = {(b.clue_id, b.root_id, b.suffix) for b in sheet_bans}
        prev_root_by_clue: dict[str, Root] = {}

        for entry in entries:
            if entry.entry_id in seen_entries:
                raise CoinageCoverageError(
                    f"duplicate coinage entry id {entry.entry_id!r} in sheet"
                )
            seen_entries.add(entry.entry_id)

            clue = self.clues.get_clue(entry.clue_id)
            if clue.document_id != document_id:
                raise CoinageCoverageError(
                    f"clue {entry.clue_id!r} belongs to {clue.document_id!r}, not {document_id!r}"
                )
            # The block must actually cover this clue: same segment and the
            # exact revision the package froze.
            if clue.segment_id not in source_ids:
                raise CoinageCoverageError(
                    f"clue {entry.clue_id!r} ({clue.segment_id}@{clue.revision}) is not "
                    f"covered by block {block_id!r} of package {package_id!r}"
                )
            frozen_rev = snapshot.get(clue.segment_id)
            if frozen_rev != clue.revision:
                raise CoinageCoverageError(
                    f"clue {entry.clue_id!r} is pinned to {clue.segment_id}@{clue.revision} "
                    f"but the package froze revision {frozen_rev}"
                )

            if entry.op in ("root", "copy"):
                if entry.op == "root":
                    root = self.clues.get_root(entry.root_id)  # type: ignore[arg-type]
                else:
                    root = prev_root_by_clue.get(entry.clue_id)  # type: ignore[assignment]
                    if root is None:
                        raise CoinageCoverageError(
                            f"entry {entry.entry_id!r} copies a root but none of clue "
                            f"{entry.clue_id!r} precedes it"
                        )
                if root.clue_id != entry.clue_id:
                    raise CoinageCoverageError(
                        f"entry {entry.entry_id!r} uses root {root.root_id!r} of clue "
                        f"{root.clue_id!r}, not {entry.clue_id!r}"
                    )
                if entry.suffix:
                    allowed = set()
                    for cid in root.suffix_class_ids:
                        allowed.update(self.clues.get_suffix_class(cid).suffixes)
                    if entry.suffix not in allowed:
                        raise CoinageCoverageError(
                            f"suffix {entry.suffix!r} is not in any registered class of "
                            f"root {root.root_id!r}"
                        )
                    if (entry.clue_id, root.root_id, entry.suffix) in ban_set or (
                        self.clues.is_banned(entry.clue_id, root.root_id, entry.suffix)
                    ):
                        raise BannedCombination(
                            f"root {root.root_id!r}+suffix {entry.suffix!r} is a banned "
                            f"combination for clue {entry.clue_id!r}"
                        )
                prev_root_by_clue[entry.clue_id] = root
            else:  # literal-only
                if entry.suffix:
                    raise CoinageCoverageError(
                        f"literal entry {entry.entry_id!r} cannot take a suffix without a root"
                    )

    # ------------------------------------------------------------------
    # sheets
    # ------------------------------------------------------------------
    def add_sheet(
        self,
        sheet_id: str,
        document_id: str,
        language: str,
        package_id: str,
        block_id: str,
        entries: Iterable[CoinageEntry | dict[str, object]],
        bans: Iterable[Ban | dict[str, object]] = (),
    ) -> CoinageSheet:
        if not sheet_id:
            raise CoinageCoverageError("sheet_id must be non-empty")
        norm_entries: list[CoinageEntry] = []
        for raw in entries:
            if isinstance(raw, CoinageEntry):
                norm_entries.append(raw)
            else:
                norm_entries.append(
                    CoinageEntry(
                        entry_id=str(raw["entry_id"]),
                        sheet_id=sheet_id,
                        clue_id=str(raw["clue_id"]),
                        op=str(raw["op"]),
                        root_id=None if raw.get("root_id") is None else str(raw["root_id"]),
                        literal=str(raw.get("literal", "")),
                        suffix=str(raw.get("suffix", "")),
                    )
                )
        norm_bans: list[Ban] = []
        for raw in bans:
            if isinstance(raw, Ban):
                norm_bans.append(raw)
            else:
                norm_bans.append(
                    Ban(
                        ban_id=str(raw["ban_id"]),
                        clue_id=str(raw["clue_id"]),
                        root_id=str(raw["root_id"]),
                        suffix=str(raw["suffix"]),
                    )
                )
        self._validate_entries(document_id, package_id, block_id, norm_entries, norm_bans)
        sha = _sheet_content_sha(
            document_id, language, package_id, block_id, norm_entries, norm_bans
        )
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                existing = conn.execute(
                    "SELECT content_sha FROM sheets WHERE sheet_id = ?", (sheet_id,)
                ).fetchone()
                if existing is not None:
                    if existing["content_sha"] != sha:
                        raise SheetImmutable(
                            f"sheet {sheet_id!r} already exists with different content"
                        )
                    return self.get_sheet(sheet_id)
                conn.execute(
                    "INSERT INTO sheets (sheet_id, document_id, language, package_id,"
                    " block_id, content_sha, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sheet_id,
                        document_id,
                        language,
                        package_id,
                        block_id,
                        sha,
                        _now(),
                    ),
                )
                for seq, entry in enumerate(norm_entries):
                    conn.execute(
                        "INSERT INTO entries (sheet_id, entry_id, seq, clue_id, op,"
                        " root_id, literal, suffix) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sheet_id,
                            entry.entry_id,
                            seq,
                            entry.clue_id,
                            entry.op,
                            entry.root_id,
                            entry.literal,
                            entry.suffix,
                        ),
                    )
                for ban in norm_bans:
                    conn.execute(
                        "INSERT INTO sheet_bans (sheet_id, ban_id, clue_id, root_id, suffix)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (sheet_id, ban.ban_id, ban.clue_id, ban.root_id, ban.suffix),
                    )
        finally:
            conn.close()
        return self.get_sheet(sheet_id)

    def get_sheet(self, sheet_id: str) -> CoinageSheet:
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM sheets WHERE sheet_id = ?", (sheet_id,)
            ).fetchone()
            if row is None:
                raise CoinageCoverageError(f"sheet {sheet_id!r} not found")
            entries = conn.execute(
                "SELECT * FROM entries WHERE sheet_id = ? ORDER BY seq", (sheet_id,)
            ).fetchall()
            bans = conn.execute(
                "SELECT * FROM sheet_bans WHERE sheet_id = ? ORDER BY ban_id", (sheet_id,)
            ).fetchall()
        finally:
            conn.close()
        return CoinageSheet(
            sheet_id=row["sheet_id"],
            document_id=row["document_id"],
            language=row["language"],
            package_id=row["package_id"],
            block_id=row["block_id"],
            entries=tuple(
                CoinageEntry(
                    entry_id=r["entry_id"],
                    sheet_id=r["sheet_id"],
                    clue_id=r["clue_id"],
                    op=r["op"],
                    root_id=r["root_id"],
                    literal=r["literal"],
                    suffix=r["suffix"],
                )
                for r in entries
            ),
            bans=tuple(
                Ban(
                    ban_id=r["ban_id"],
                    clue_id=r["clue_id"],
                    root_id=r["root_id"],
                    suffix=r["suffix"],
                )
                for r in bans
            ),
        )

    def list_sheets(
        self,
        document_id: Optional[str] = None,
        language: Optional[str] = None,
        package_id: Optional[str] = None,
        block_id: Optional[str] = None,
    ) -> list[CoinageSheet]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("document_id", document_id),
            ("language", language),
            ("package_id", package_id),
            ("block_id", block_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = _connect(self.db_path)
        try:
            ids = [
                r["sheet_id"]
                for r in conn.execute(
                    "SELECT sheet_id FROM sheets" + where + " ORDER BY sheet_id", params
                ).fetchall()
            ]
        finally:
            conn.close()
        return [self.get_sheet(sid) for sid in ids]

    def assert_block(self, sheet_id: str, package_id: str, block_id: str) -> None:
        """Refuse to apply a sheet to a different package/block than it was cut for."""

        sheet = self.get_sheet(sheet_id)
        if (sheet.package_id, sheet.block_id) != (package_id, block_id):
            raise SheetBlockMismatch(
                f"sheet {sheet_id!r} was cut for ({sheet.package_id!r}, "
                f"{sheet.block_id!r}), cannot apply to ({package_id!r}, {block_id!r})"
            )

    # ------------------------------------------------------------------
    # withdrawals
    # ------------------------------------------------------------------
    def is_withdrawn(self, sheet_id: str, entry_id: str) -> bool:
        conn = _connect(self.db_path)
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM withdrawals WHERE sheet_id = ? AND entry_id = ?",
                    (sheet_id, entry_id),
                ).fetchone()
                is not None
            )
        finally:
            conn.close()

    def withdrawn_entries(self, sheet_id: str) -> set[str]:
        conn = _connect(self.db_path)
        try:
            return {
                r["entry_id"]
                for r in conn.execute(
                    "SELECT entry_id FROM withdrawals WHERE sheet_id = ?", (sheet_id,)
                ).fetchall()
            }
        finally:
            conn.close()

    def withdraw_coinage(
        self, sheet_id: str, entry_id: str, reason: str = ""
    ) -> None:
        """Record a tombstone for one coinage; the stored sheet is untouched."""

        sheet = self.get_sheet(sheet_id)
        if entry_id not in {e.entry_id for e in sheet.entries}:
            raise CoinageCoverageError(
                f"entry {entry_id!r} not found in sheet {sheet_id!r}"
            )
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO withdrawals (sheet_id, entry_id, withdrawn_at,"
                    " reason) VALUES (?, ?, ?, ?)",
                    (sheet_id, entry_id, _now(), reason),
                )
        finally:
            conn.close()

    def withdraw_spelling(
        self, sheet_id: str, clue_id: str, spelling: str, reason: str = ""
    ) -> list[str]:
        """Withdraw every coinage in a sheet that yields ``spelling`` for a clue."""

        sheet = self.get_sheet(sheet_id)
        targets = [
            entry_id
            for entry_id, cid, candidate in resolve_sheet_entries(sheet, self.clues)
            if cid == clue_id and candidate == spelling
        ]
        if not targets:
            raise CoinageCoverageError(
                f"sheet {sheet_id!r} has no coinage of {clue_id!r} spelling {spelling!r}"
            )
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                for entry_id in targets:
                    conn.execute(
                        "INSERT OR REPLACE INTO withdrawals (sheet_id, entry_id,"
                        " withdrawn_at, reason) VALUES (?, ?, ?, ?)",
                        (sheet_id, entry_id, _now(), reason),
                    )
        finally:
            conn.close()
        return targets

    # ------------------------------------------------------------------
    # issued proofs (sent to print) — immutable, preserve original provenance
    # ------------------------------------------------------------------
    def issue_proof(
        self, proof_id: str, package_id: str, block_id: str, content: dict[str, object]
    ) -> dict[str, object]:
        sha = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                existing = conn.execute(
                    "SELECT content_sha, content_json FROM issued_proofs WHERE proof_id = ?",
                    (proof_id,),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha"] != sha:
                        raise SheetImmutable(
                            f"proof {proof_id!r} already issued with different content"
                        )
                    return json.loads(existing["content_json"])
                conn.execute(
                    "INSERT INTO issued_proofs (proof_id, package_id, block_id,"
                    " content_json, content_sha, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        proof_id,
                        package_id,
                        block_id,
                        json.dumps(content, ensure_ascii=False, sort_keys=True),
                        sha,
                        _now(),
                    ),
                )
        finally:
            conn.close()
        return content

    def get_proof(self, proof_id: str) -> dict[str, object]:
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT content_json FROM issued_proofs WHERE proof_id = ?", (proof_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise CoinageCoverageError(f"proof {proof_id!r} not found")
        return json.loads(row["content_json"])

    def list_proofs(self) -> list[dict[str, object]]:
        conn = _connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT proof_id, package_id, block_id, content_sha, created_at"
                " FROM issued_proofs ORDER BY created_at, proof_id"
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "proof_id": r["proof_id"],
                "package_id": r["package_id"],
                "block_id": r["block_id"],
                "content_sha": r["content_sha"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def open_coinage_repository(
    coinage_db: str, packages: PackageReader, clues: ClueReader
) -> CoinageRepository:
    return CoinageRepository(coinage_db, packages, clues)

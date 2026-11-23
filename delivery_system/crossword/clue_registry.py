"""Source-side clue registry.

Every clue (谜面) is pinned to an exact ``(segment_id, revision)`` of a source
document.  On that revision the editor assigns a sense number, registers roots
and their usable suffix classes, and records audited root+suffix bans.

This side knows nothing about coinage sheets, packages or boards.  It is the
authoritative side for *what may be coined* for a sense.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional, Protocol

from .errors import (
    ClueExists,
    ClueNotFound,
    RootNotFound,
    SuffixClassNotFound,
)
from .models import Ban, ClueSense, Root, SuffixClass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clues (
    clue_id      TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL,
    segment_id   TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    sense_no     TEXT NOT NULL,
    surface      TEXT NOT NULL,
    UNIQUE (document_id, segment_id, revision, sense_no)
);
CREATE TABLE IF NOT EXISTS suffix_classes (
    class_id     TEXT PRIMARY KEY,
    clue_id      TEXT NOT NULL REFERENCES clues(clue_id) ON DELETE CASCADE,
    suffixes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roots (
    root_id      TEXT PRIMARY KEY,
    clue_id      TEXT NOT NULL REFERENCES clues(clue_id) ON DELETE CASCADE,
    letters      TEXT NOT NULL,
    suffix_class_ids_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bans (
    ban_id       TEXT PRIMARY KEY,
    clue_id      TEXT NOT NULL REFERENCES clues(clue_id) ON DELETE CASCADE,
    root_id      TEXT NOT NULL,
    suffix       TEXT NOT NULL,
    UNIQUE (clue_id, root_id, suffix)
);
"""


class SourceRevisionReader(Protocol):
    def published_product(self, document_id: str): ...


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(os.fspath(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class ClueRegistry:
    """Read/write clue registry backed by its own SQLite database."""

    def __init__(self, db_path: str, source: Optional[SourceRevisionReader] = None) -> None:
        self.db_path = os.fspath(db_path)
        self.source = source
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
    # editor entries
    # ------------------------------------------------------------------
    def add_clue(
        self,
        clue_id: str,
        document_id: str,
        segment_id: str,
        revision: int,
        sense_no: str,
        surface: str = "",
    ) -> ClueSense:
        if not clue_id:
            raise ClueNotFound("clue_id must be non-empty")
        if revision < 1:
            raise ClueNotFound("revision must be >= 1")
        if self.source is not None:
            product = self.source.published_product(document_id)
            match = [
                seg
                for seg in product.segments
                if seg.segment_id == segment_id and seg.revision == revision
            ]
            if not match:
                raise ClueNotFound(
                    f"source segment {segment_id!r}@{revision} not published for "
                    f"{document_id!r}; a clue must be pinned to an existing revision"
                )
        clue = ClueSense(
            clue_id=clue_id,
            document_id=document_id,
            segment_id=segment_id,
            revision=revision,
            sense_no=sense_no,
            surface=surface,
        )
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                existing = conn.execute(
                    "SELECT 1 FROM clues WHERE clue_id = ?", (clue_id,)
                ).fetchone()
                if existing:
                    raise ClueExists(f"clue {clue_id!r} already exists")
                conn.execute(
                    "INSERT INTO clues (clue_id, document_id, segment_id, revision,"
                    " sense_no, surface) VALUES (?, ?, ?, ?, ?, ?)",
                    (clue_id, document_id, segment_id, revision, sense_no, surface),
                )
        finally:
            conn.close()
        return clue

    def add_suffix_class(
        self, class_id: str, clue_id: str, suffixes: Iterable[str]
    ) -> SuffixClass:
        suffixes = tuple(suffixes)
        if not suffixes:
            raise SuffixClassNotFound("a suffix class needs at least one suffix")
        obj = SuffixClass(class_id=class_id, clue_id=clue_id, suffixes=suffixes)
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                self._require_clue(conn, clue_id)
                if conn.execute(
                    "SELECT 1 FROM suffix_classes WHERE class_id = ?", (class_id,)
                ).fetchone():
                    raise SuffixClassNotFound(f"suffix class {class_id!r} already exists")
                conn.execute(
                    "INSERT INTO suffix_classes (class_id, clue_id, suffixes_json)"
                    " VALUES (?, ?, ?)",
                    (class_id, clue_id, json.dumps(list(suffixes), ensure_ascii=False)),
                )
        finally:
            conn.close()
        return obj

    def add_root(
        self, root_id: str, clue_id: str, letters: str, suffix_class_ids: Iterable[str]
    ) -> Root:
        class_ids = tuple(suffix_class_ids)
        if not letters:
            raise RootNotFound("a root needs at least one letter")
        root = Root(
            root_id=root_id,
            clue_id=clue_id,
            letters=letters,
            suffix_class_ids=class_ids,
        )
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                self._require_clue(conn, clue_id)
                for cid in class_ids:
                    row = conn.execute(
                        "SELECT clue_id FROM suffix_classes WHERE class_id = ?", (cid,)
                    ).fetchone()
                    if row is None:
                        raise SuffixClassNotFound(
                            f"root {root_id!r} references unknown suffix class {cid!r}"
                        )
                    if row["clue_id"] != clue_id:
                        raise SuffixClassNotFound(
                            f"suffix class {cid!r} belongs to clue {row['clue_id']!r},"
                            f" not {clue_id!r}"
                        )
                if conn.execute(
                    "SELECT 1 FROM roots WHERE root_id = ?", (root_id,)
                ).fetchone():
                    raise RootNotFound(f"root {root_id!r} already exists")
                conn.execute(
                    "INSERT INTO roots (root_id, clue_id, letters, suffix_class_ids_json)"
                    " VALUES (?, ?, ?, ?)",
                    (root_id, clue_id, letters, json.dumps(list(class_ids))),
                )
        finally:
            conn.close()
        return root

    def add_ban(self, ban_id: str, clue_id: str, root_id: str, suffix: str) -> Ban:
        ban = Ban(ban_id=ban_id, clue_id=clue_id, root_id=root_id, suffix=suffix)
        conn = _connect(self.db_path)
        try:
            with self._txn(conn):
                self._require_clue(conn, clue_id)
                root = conn.execute(
                    "SELECT clue_id FROM roots WHERE root_id = ?", (root_id,)
                ).fetchone()
                if root is None or root["clue_id"] != clue_id:
                    raise RootNotFound(
                        f"ban references root {root_id!r} not registered for {clue_id!r}"
                    )
                try:
                    conn.execute(
                        "INSERT INTO bans (ban_id, clue_id, root_id, suffix)"
                        " VALUES (?, ?, ?, ?)",
                        (ban_id, clue_id, root_id, suffix),
                    )
                except sqlite3.IntegrityError:
                    raise ClueExists(
                        f"ban on {root_id!r}+{suffix!r} already recorded for {clue_id!r}"
                    )
        finally:
            conn.close()
        return ban

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get_clue(self, clue_id: str) -> ClueSense:
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM clues WHERE clue_id = ?", (clue_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ClueNotFound(f"clue {clue_id!r} not found")
        return self._clue_from_row(row)

    def list_clues(self, document_id: Optional[str] = None) -> list[ClueSense]:
        conn = _connect(self.db_path)
        try:
            if document_id is None:
                rows = conn.execute("SELECT * FROM clues ORDER BY clue_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM clues WHERE document_id = ? ORDER BY clue_id",
                    (document_id,),
                ).fetchall()
        finally:
            conn.close()
        return [self._clue_from_row(r) for r in rows]

    def get_root(self, root_id: str) -> Root:
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM roots WHERE root_id = ?", (root_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise RootNotFound(f"root {root_id!r} not found")
        return self._root_from_row(row)

    def roots_for(self, clue_id: str) -> list[Root]:
        conn = _connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM roots WHERE clue_id = ? ORDER BY root_id", (clue_id,)
            ).fetchall()
        finally:
            conn.close()
        return [self._root_from_row(r) for r in rows]

    def get_suffix_class(self, class_id: str) -> SuffixClass:
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM suffix_classes WHERE class_id = ?", (class_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise SuffixClassNotFound(f"suffix class {class_id!r} not found")
        return SuffixClass(
            class_id=row["class_id"],
            clue_id=row["clue_id"],
            suffixes=tuple(json.loads(row["suffixes_json"])),
        )

    def suffix_classes_for(self, clue_id: str) -> list[SuffixClass]:
        conn = _connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM suffix_classes WHERE clue_id = ? ORDER BY class_id",
                (clue_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            SuffixClass(
                class_id=r["class_id"],
                clue_id=r["clue_id"],
                suffixes=tuple(json.loads(r["suffixes_json"])),
            )
            for r in rows
        ]

    def bans_for(self, clue_id: str) -> list[Ban]:
        conn = _connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM bans WHERE clue_id = ? ORDER BY ban_id", (clue_id,)
            ).fetchall()
        finally:
            conn.close()
        return [
            Ban(
                ban_id=r["ban_id"],
                clue_id=r["clue_id"],
                root_id=r["root_id"],
                suffix=r["suffix"],
            )
            for r in rows
        ]

    def is_banned(self, clue_id: str, root_id: str, suffix: str) -> bool:
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM bans WHERE clue_id = ? AND root_id = ? AND suffix = ?",
                (clue_id, root_id, suffix),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _require_clue(conn: sqlite3.Connection, clue_id: str) -> None:
        if conn.execute(
            "SELECT 1 FROM clues WHERE clue_id = ?", (clue_id,)
        ).fetchone() is None:
            raise ClueNotFound(f"clue {clue_id!r} not found")

    @staticmethod
    def _clue_from_row(row: sqlite3.Row) -> ClueSense:
        return ClueSense(
            clue_id=row["clue_id"],
            document_id=row["document_id"],
            segment_id=row["segment_id"],
            revision=int(row["revision"]),
            sense_no=row["sense_no"],
            surface=row["surface"],
        )

    @staticmethod
    def _root_from_row(row: sqlite3.Row) -> Root:
        return Root(
            root_id=row["root_id"],
            clue_id=row["clue_id"],
            letters=row["letters"],
            suffix_class_ids=tuple(json.loads(row["suffix_class_ids_json"])),
        )


class PublishedClueReader:
    """Read-only handle over a clue registry database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.fspath(db_path)
        if not os.path.exists(self.db_path):
            raise ClueNotFound(f"clue database {self.db_path!r} does not exist")

    def get_clue(self, clue_id: str) -> ClueSense:
        return ClueRegistry(self.db_path).get_clue(clue_id)

    def list_clues(self, document_id: Optional[str] = None) -> list[ClueSense]:
        return ClueRegistry(self.db_path).list_clues(document_id)

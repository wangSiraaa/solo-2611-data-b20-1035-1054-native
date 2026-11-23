"""Terminology office (术语室): approved Chinese/foreign name pairs per course.

Each local course may have several *approved term pairs*.  A pair carries

* ``zh`` / ``fl``        -- the Chinese and foreign names printed on a sheet,
* ``zh_channel`` / ``fl_channel`` -- the pre-registered colour channel each
  half of the pair is printed with (the two halves may use different
  channels), and
* a set of *approved groups* -- the requirement groups in which this pair may
  be used.

Sheet-level naming rules (enforced by :func:`check_naming` /
:func:`solve_namings`):

1. every row uses a pair approved for *that row's* group;
2. a course appearing in several rows (hence possibly several groups) must
   use the *same* pair everywhere, and that pair must be approved for every
   group the course actually appears in;
3. two different courses may not be printed as the same complete pair
   (identical Chinese *and* foreign text).

Approvals can be withdrawn and restored; every change is logged in
``term_events`` so the office can explain later why a naming that used to be
legal no longer is.  The office can also explain the "every row individually
has a name, yet the whole book has no solution" situation via
:func:`explain_naming`.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .errors import TermError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# pair model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TermPair:
    pair_id: str
    course_id: str
    zh: str
    fl: str
    zh_channel: str
    fl_channel: str

    def as_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "course_id": self.course_id,
            "zh": self.zh,
            "fl": self.fl,
            "zh_channel": self.zh_channel,
            "fl_channel": self.fl_channel,
        }


# --------------------------------------------------------------------------- #
# the office (SQLite-backed registry of pairs + per-group approvals)
# --------------------------------------------------------------------------- #
class TermOffice:
    """Persistent registry of approved term pairs.

    The DB is a single file; pass ``:memory:`` for an ephemeral office.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS term_pairs (
                pair_id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL,
                zh TEXT NOT NULL,
                fl TEXT NOT NULL,
                zh_channel TEXT NOT NULL,
                fl_channel TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS term_pair_groups (
                pair_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                withdrawn_at TEXT,
                PRIMARY KEY (pair_id, group_id),
                FOREIGN KEY (pair_id) REFERENCES term_pairs(pair_id)
            );
            CREATE TABLE IF NOT EXISTS term_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                pair_id TEXT,
                group_id TEXT,
                detail_json TEXT NOT NULL,
                at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TermOffice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _event(self, kind: str, pair_id: Optional[str], group_id: Optional[str], detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO term_events(kind, pair_id, group_id, detail_json, at) VALUES (?,?,?,?,?)",
            (kind, pair_id, group_id, json.dumps(detail, ensure_ascii=False, sort_keys=True), _now()),
        )

    # -- registration ------------------------------------------------------
    def register_pair(
        self,
        *,
        pair_id: str,
        course_id: str,
        zh: str,
        fl: str,
        zh_channel: str,
        fl_channel: str,
        approved_groups: Sequence[str],
    ) -> TermPair:
        """Register a pair with its pre-registered channels and group approvals."""
        for name, val in (("pair_id", pair_id), ("course_id", course_id), ("zh", zh), ("fl", fl),
                          ("zh_channel", zh_channel), ("fl_channel", fl_channel)):
            if not (isinstance(val, str) and val.strip()):
                raise TermError(f"{name} must be a non-empty string", location=name)
        if not approved_groups:
            raise TermError("a pair must be approved for at least one group", location=pair_id)
        existing = self.get_pair(pair_id)
        if existing is not None:
            raise TermError(f"pair {pair_id!r} is already registered", location=pair_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO term_pairs(pair_id, course_id, zh, fl, zh_channel, fl_channel, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (pair_id, course_id, zh, fl, zh_channel, fl_channel, _now()),
            )
            for gid in dict.fromkeys(approved_groups):
                self.conn.execute(
                    "INSERT INTO term_pair_groups(pair_id, group_id, withdrawn_at) VALUES (?,?,NULL)",
                    (pair_id, gid),
                )
            self._event("registered", pair_id, None, {
                "course_id": course_id, "zh": zh, "fl": fl,
                "zh_channel": zh_channel, "fl_channel": fl_channel,
                "approved_groups": list(dict.fromkeys(approved_groups)),
            })
        return TermPair(pair_id, course_id, zh, fl, zh_channel, fl_channel)

    def get_pair(self, pair_id: str) -> Optional[TermPair]:
        row = self.conn.execute(
            "SELECT pair_id, course_id, zh, fl, zh_channel, fl_channel FROM term_pairs WHERE pair_id = ?",
            (pair_id,),
        ).fetchone()
        if row is None:
            return None
        return TermPair(row["pair_id"], row["course_id"], row["zh"], row["fl"],
                        row["zh_channel"], row["fl_channel"])

    # -- approvals -----------------------------------------------------------
    def approved_groups(self, pair_id: str) -> FrozenSet[str]:
        rows = self.conn.execute(
            "SELECT group_id FROM term_pair_groups WHERE pair_id = ? AND withdrawn_at IS NULL",
            (pair_id,),
        ).fetchall()
        return frozenset(r["group_id"] for r in rows)

    def withdraw_approval(self, pair_id: str, group_id: str) -> None:
        """Withdraw a pair's approval for one group (kept in history)."""
        if self.get_pair(pair_id) is None:
            raise TermError(f"unknown pair {pair_id!r}", location=pair_id)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE term_pair_groups SET withdrawn_at = ? "
                "WHERE pair_id = ? AND group_id = ? AND withdrawn_at IS NULL",
                (_now(), pair_id, group_id),
            )
            if cur.rowcount == 0:
                raise TermError(
                    f"pair {pair_id!r} has no live approval for group {group_id!r}",
                    location=pair_id,
                )
            self._event("approval_withdrawn", pair_id, group_id, {})

    def restore_approval(self, pair_id: str, group_id: str) -> None:
        """Restore a previously withdrawn approval."""
        if self.get_pair(pair_id) is None:
            raise TermError(f"unknown pair {pair_id!r}", location=pair_id)
        with self.conn:
            row = self.conn.execute(
                "SELECT withdrawn_at FROM term_pair_groups WHERE pair_id = ? AND group_id = ?",
                (pair_id, group_id),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO term_pair_groups(pair_id, group_id, withdrawn_at) VALUES (?,?,NULL)",
                    (pair_id, group_id),
                )
            elif row["withdrawn_at"] is not None:
                self.conn.execute(
                    "UPDATE term_pair_groups SET withdrawn_at = NULL WHERE pair_id = ? AND group_id = ?",
                    (pair_id, group_id),
                )
            else:
                return  # already live: idempotent
            self._event("approval_restored", pair_id, group_id, {})

    def pairs_for_course(self, course_id: str) -> List[TermPair]:
        rows = self.conn.execute(
            "SELECT pair_id FROM term_pairs WHERE course_id = ? ORDER BY pair_id", (course_id,),
        ).fetchall()
        return [self.get_pair(r["pair_id"]) for r in rows]

    def approved_pairs(self, course_id: str, group_id: str) -> List[TermPair]:
        """Pairs of ``course_id`` currently approved for ``group_id``."""
        return [p for p in self.pairs_for_course(course_id)
                if group_id in self.approved_groups(p.pair_id)]

    def events(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT event_id, kind, pair_id, group_id, detail_json, at FROM term_events ORDER BY event_id"
        ).fetchall()
        return [
            {"event_id": r["event_id"], "kind": r["kind"], "pair_id": r["pair_id"],
             "group_id": r["group_id"], "detail": json.loads(r["detail_json"]), "at": r["at"]}
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# naming logic over a master sheet (pure functions; sheet is a duck-typed
# object with a ``rows`` list of objects exposing record_id/course_id/group_id)
# --------------------------------------------------------------------------- #
def _reason(code: str, message: str, location: Optional[str] = None, detail=None) -> dict:
    r = {"code": code, "message": message}
    if location is not None:
        r["location"] = location
    if detail is not None:
        r["detail"] = detail
    return r


def course_groups(sheet) -> Dict[str, List[str]]:
    """Course -> sorted list of the distinct groups it actually appears in."""
    out: Dict[str, set] = {}
    for row in sheet.rows:
        out.setdefault(row.course_id, set()).add(row.group_id)
    return {cid: sorted(gs) for cid, gs in out.items()}


def row_approvable_pairs(office: TermOffice, sheet) -> Dict[str, List[TermPair]]:
    """Per record_id: pairs approved for that row's own group (row-local view)."""
    out: Dict[str, List[TermPair]] = {}
    for row in sheet.rows:
        out[row.record_id] = office.approved_pairs(row.course_id, row.group_id)
    return out


def course_candidate_pairs(office: TermOffice, sheet) -> Dict[str, List[TermPair]]:
    """Course -> pairs approved for *every* group the course appears in."""
    out: Dict[str, List[TermPair]] = {}
    for cid, groups in course_groups(sheet).items():
        cands = []
        for pair in office.pairs_for_course(cid):
            approvals = office.approved_groups(pair.pair_id)
            if all(g in approvals for g in groups):
                cands.append(pair)
        out[cid] = cands
    return out


def check_naming(office: TermOffice, sheet, row_choices: Dict[str, str]) -> List[dict]:
    """Check a *row-level* naming proposal ``{record_id: pair_id}``.

    Row-level on purpose: the office must be able to reject a proposal that
    gives one course two different translations on two rows
    (``course_term_inconsistent``) and one that prints two courses as the same
    complete pair (``name_collision``).  Returns a list of located reasons;
    empty means the naming is legal.
    """
    reasons: List[dict] = []
    rows = {row.record_id: row for row in sheet.rows}

    chosen: Dict[str, TermPair] = {}
    for record_id, pair_id in sorted(row_choices.items()):
        row = rows.get(record_id)
        if row is None:
            reasons.append(_reason(
                "unknown_record",
                f"record {record_id!r} is not on sheet {sheet.sheet_id}",
                location=record_id,
            ))
            continue
        pair = office.get_pair(pair_id)
        if pair is None:
            reasons.append(_reason(
                "unknown_pair",
                f"pair {pair_id!r} is not registered with the terminology office",
                location=record_id,
            ))
            continue
        if pair.course_id != row.course_id:
            reasons.append(_reason(
                "pair_course_mismatch",
                f"pair {pair_id!r} names course {pair.course_id!r}, not {row.course_id!r}",
                location=record_id,
            ))
            continue
        chosen[record_id] = pair
        if row.group_id not in office.approved_groups(pair_id):
            reasons.append(_reason(
                "pair_not_approved_for_group",
                f"pair {pair_id!r} is not approved for group {row.group_id!r}",
                location=record_id,
                detail={"pair_id": pair_id, "group_id": row.group_id},
            ))

    # every row must be named
    for record_id in sorted(rows):
        if record_id not in row_choices:
            reasons.append(_reason(
                "row_unnamed",
                f"record {record_id!r} has no chosen pair",
                location=record_id,
            ))

    # same course -> same pair on every row it appears
    per_course: Dict[str, Dict[str, List[str]]] = {}
    for record_id, pair in chosen.items():
        per_course.setdefault(pair.course_id, {}).setdefault(pair.pair_id, []).append(record_id)
    for cid, by_pair in sorted(per_course.items()):
        if len(by_pair) > 1:
            reasons.append(_reason(
                "course_term_inconsistent",
                f"course {cid!r} is printed with {len(by_pair)} different pairs; "
                f"one course must use one pair across the whole sheet",
                location=cid,
                detail={pid: sorted(rids) for pid, rids in sorted(by_pair.items())},
            ))

    # two different courses may not print as the same complete pair
    by_text: Dict[Tuple[str, str], List[str]] = {}
    for record_id, pair in chosen.items():
        by_text.setdefault((pair.zh, pair.fl), []).append(pair.course_id)
    for (zh, fl), cids in sorted(by_text.items()):
        distinct = sorted(set(cids))
        if len(distinct) > 1:
            reasons.append(_reason(
                "name_collision",
                f"courses {distinct} would all be printed as {zh!r} / {fl!r}; "
                f"two different courses may not share one complete pair",
                location=distinct[0],
                detail={"courses": distinct, "zh": zh, "fl": fl},
            ))
    return reasons


def solve_namings(office: TermOffice, sheet, *, limit: int = 64) -> List[Dict[str, str]]:
    """Enumerate legal course-level namings ``{course_id: pair_id}``.

    Course-level by construction, so rule 2 (one course, one pair) is
    structural; rule 3 (no cross-course identical text) is filtered here.
    Deterministic order: courses and pairs sorted by id.
    """
    candidates = course_candidate_pairs(office, sheet)
    courses = sorted(candidates)
    if any(not candidates[cid] for cid in courses):
        return []
    out: List[Dict[str, str]] = []
    for combo in itertools.product(*(candidates[cid] for cid in courses)):
        texts = [(p.zh, p.fl) for p in combo]
        if len(set(texts)) != len(texts):
            continue  # name_collision
        out.append({cid: p.pair_id for cid, p in zip(courses, combo)})
        if len(out) >= limit:
            break
    return out


def explain_naming(office: TermOffice, sheet) -> dict:
    """Explain why a sheet has no legal naming (or confirm that it has one).

    Distinguishes the interesting case -- every *row* individually has at
    least one approved pair, yet the whole sheet has no legal naming -- and
    pinpoints why: either a course's appearances span groups with no single
    pair approved for all of them, or the only candidate pairs force two
    courses onto identical printed text.
    """
    per_row = row_approvable_pairs(office, sheet)
    per_course = course_candidate_pairs(office, sheet)
    namings = solve_namings(office, sheet)

    rows_report = {
        rid: [p.pair_id for p in pairs] for rid, pairs in sorted(per_row.items())
    }
    courses_report = {
        cid: {
            "groups": course_groups(sheet)[cid],
            "pairs_covering_all_groups": [p.pair_id for p in pairs],
        }
        for cid, pairs in sorted(per_course.items())
    }

    reasons: List[dict] = []
    for rid, pairs in sorted(per_row.items()):
        if not pairs:
            row = next(r for r in sheet.rows if r.record_id == rid)
            reasons.append(_reason(
                "row_without_terms",
                f"record {rid!r} (course {row.course_id!r}, group {row.group_id!r}) has no "
                f"approved pair at all",
                location=rid,
            ))
    every_row_has_terms = not reasons

    if every_row_has_terms and not namings:
        # the "each row has a word, the book has none" case
        for cid, pairs in sorted(per_course.items()):
            groups = course_groups(sheet)[cid]
            if not pairs:
                per_group = {
                    g: [p.pair_id for p in office.approved_pairs(cid, g)] for g in groups
                }
                reasons.append(_reason(
                    "cross_group_pair_conflict",
                    f"course {cid!r} appears in groups {groups}; no single approved pair "
                    f"covers all of them, and one course must use one pair everywhere",
                    location=cid,
                    detail={"approved_pairs_per_group": per_group},
                ))
        if not reasons:
            # every course has candidates, yet all combinations collide
            texts: Dict[Tuple[str, str], List[str]] = {}
            for cid, pairs in sorted(per_course.items()):
                for p in pairs:
                    texts.setdefault((p.zh, p.fl), [])
                    texts[(p.zh, p.fl)].append(cid)
            forced = {
                f"{zh}|{fl}": sorted(set(cids))
                for (zh, fl), cids in texts.items() if len(set(cids)) > 1
            }
            reasons.append(_reason(
                "forced_name_collision",
                "every per-course candidate combination prints two different courses as "
                "the same complete pair",
                detail={"shared_texts": forced},
            ))

    return {
        "sheet_id": sheet.sheet_id,
        "solvable": bool(namings),
        "namings": namings,
        "row_options": rows_report,
        "course_options": courses_report,
        "reasons": reasons,
    }


__all__ = [
    "TermPair",
    "TermOffice",
    "check_naming",
    "solve_namings",
    "explain_naming",
    "course_groups",
    "course_candidate_pairs",
    "row_approvable_pairs",
]

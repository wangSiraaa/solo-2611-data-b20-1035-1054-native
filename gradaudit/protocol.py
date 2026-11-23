"""Credit protocol file (协议档) keyed by the *chosen local course*.

For every local course that can issue transferable credit, the protocol records:

* ``issuing_school`` -- which school issues that credit (发行学校),
* ``kind``           -- the transferable-credit kind / category (可转学时种类),
* ``ratio``          -- exact credits per transferred point (兑换比例).

The ledger builder (:mod:`gradaudit.ledger`) looks up the protocol by the local
course that the *confirmed* allocation actually selected -- never by course
name and never by a course the student might prefer instead.

Document ``credit-protocol/1``::

    {
      "format": "credit-protocol/1",
      "revision": 1,
      "entries": [
        {"course_id": "PU_A1", "issuing_school": "PU", "kind": "A", "ratio": "3/2"}
      ]
    }

Ratios are exact positive rationals (see :mod:`gradaudit.rationals`); floats are
rejected so a pool floor never depends on a binary approximation.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict

from .errors import ProtocolError, SchemaError
from .model import content_hash
from .rationals import format_ratio, parse_positive_rational

PROTOCOL_FORMAT = "credit-protocol/1"


@dataclass(frozen=True)
class ProtocolEntry:
    course_id: str
    issuing_school: str
    kind: str
    ratio: Fraction

    def as_dict(self) -> dict:
        return {
            "course_id": self.course_id,
            "issuing_school": self.issuing_school,
            "kind": self.kind,
            "ratio": format_ratio(self.ratio),
        }


@dataclass(frozen=True)
class Protocol:
    revision: int
    entries: Dict[str, ProtocolEntry]   # course_id -> entry

    def entry(self, course_id: str) -> ProtocolEntry:
        try:
            return self.entries[course_id]
        except KeyError:
            raise ProtocolError(
                f"local course {course_id!r} has no credit-protocol entry; cannot time its credits",
                location=course_id,
            ) from None

    def raw_document(self) -> dict:
        return {
            "format": PROTOCOL_FORMAT,
            "revision": self.revision,
            "entries": [self.entries[cid].as_dict() for cid in sorted(self.entries)],
        }

    def content_hash(self) -> str:
        return content_hash(self.raw_document())


def _require(cond: bool, message: str, *, location: str | None = None) -> None:
    if not cond:
        raise SchemaError(message, location=location)


def parse_protocol(doc: Any) -> Protocol:
    _require(isinstance(doc, dict), "protocol must be a JSON object")
    _require(doc.get("format") == PROTOCOL_FORMAT, f"protocol format must be {PROTOCOL_FORMAT!r}", location="format")
    revision = doc.get("revision")
    _require(
        isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
        "protocol revision must be a positive integer",
        location="revision",
    )
    raw_entries = doc.get("entries")
    _require(isinstance(raw_entries, list) and raw_entries, "entries must be a non-empty list", location="entries")
    entries: Dict[str, ProtocolEntry] = {}
    for idx, raw in enumerate(raw_entries):
        loc = f"entries[{idx}]"
        _require(isinstance(raw, dict), "protocol entry must be an object", location=loc)
        cid = raw.get("course_id")
        _require(isinstance(cid, str) and cid.strip() != "", "course_id must be a non-empty string", location=loc)
        _require(cid not in entries, f"duplicate protocol entry for course {cid!r}", location=cid)
        school = raw.get("issuing_school")
        _require(isinstance(school, str) and school.strip() != "", "issuing_school must be a non-empty string", location=cid)
        kind = raw.get("kind")
        _require(isinstance(kind, str) and kind.strip() != "", "kind must be a non-empty string", location=cid)
        ratio = parse_positive_rational(raw.get("ratio"), location=cid)
        entries[cid] = ProtocolEntry(course_id=cid, issuing_school=school, kind=kind, ratio=ratio)
    return Protocol(revision=revision, entries=entries)


__all__ = ["Protocol", "ProtocolEntry", "parse_protocol", "PROTOCOL_FORMAT"]

"""Proofreader marks -> ordered, numbered headwords read from delivered packages.

Headwords never come from free text: a package is a real
``translation-package/1`` product, and every headword is an **exact character
interval** inside one delivered block's text, together with the *source segment
revision* it was proofread against.

A block that violates any of these is rejected before it can contaminate a word
list (:class:`~delivery_system.errors.CollationError` subclasses):

* interval out of bounds / empty  -> :class:`IntervalOutOfBounds` / malformed;
* interval cannot be split fully by the local type matrix -> :class:`Untokenizable`;
* an old translation block pasted onto a different edition -> :class:`StalePackage`
  (the mark's stated source revision disagrees with the block's frozen revision,
  or with the edition the caller expects).

The order of one old word list is exactly the *actual block appearance order*
followed by *within-block interval start* -- the brief's rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from ..errors import (
    IntervalOutOfBounds,
    MalformedMark,
    StalePackage,
    UnknownBlock,
)
from .glyphs import TypeMatrix, entry_id_key


@dataclass(frozen=True)
class DeliveryBlock:
    """One delivered translation block (chunk) from a real package."""

    translation_id: str
    text: str
    source_ids: tuple[str, ...]
    source_revisions: Mapping[str, int]
    position: int  # zero-based position in the package's chunk order


def blocks_from_package(content: Mapping[str, object]) -> list[DeliveryBlock]:
    """Read the real chunks of a translation-package/1 content dict.

    Revisions come from each chunk's own ``source_revisions`` when present,
    otherwise from the frozen ``source_snapshot`` keyed by ``source_ids``.
    """

    snapshot_rev: dict[str, int] = {}
    snapshot = content.get("source_snapshot")
    if isinstance(snapshot, list):
        for item in snapshot:
            if isinstance(item, dict) and "segment_id" in item and "revision" in item:
                snapshot_rev[str(item["segment_id"])] = int(item["revision"])

    chunks = content.get("chunks")
    if not isinstance(chunks, list):
        raise MalformedMark("package has no chunks")
    blocks: list[DeliveryBlock] = []
    for pos, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise MalformedMark("malformed package chunk")
        tid = str(chunk.get("translation_id", ""))
        if not tid:
            raise MalformedMark("package chunk without translation_id")
        text = chunk.get("text")
        if not isinstance(text, str):
            raise MalformedMark(f"chunk {tid!r} has no text")
        ids = tuple(str(x) for x in (chunk.get("source_ids") or ()))
        revisions: dict[str, int] = {}
        raw_revs = chunk.get("source_revisions")
        if isinstance(raw_revs, list) and len(raw_revs) == len(ids):
            for sid, rev in zip(ids, raw_revs):
                revisions[sid] = int(rev)
        else:
            for sid in ids:
                if sid in snapshot_rev:
                    revisions[sid] = snapshot_rev[sid]
        blocks.append(
            DeliveryBlock(
                translation_id=tid,
                text=text,
                source_ids=ids,
                source_revisions=revisions,
                position=pos,
            )
        )
    return blocks


@dataclass(frozen=True)
class HeadwordMark:
    """A proofreader's mark for one numbered headword inside a delivered block.

    ``start``/``end`` are a zero-based half-open character interval into the
    block text.  ``source_revision`` is the revision the proofreader claims the
    block belongs to; if it disagrees with the block's frozen revision the block
    is a stale paste.
    """

    entry_id: str
    translation_id: str
    start: int
    end: int
    segment_id: str
    source_revision: Optional[int] = None


@dataclass(frozen=True)
class Headword:
    """One real headword: its exact text, its sorts, and its provenance."""

    entry_id: str
    text: str
    tokens: tuple[str, ...]
    block_id: str
    segment_id: str
    revision: int
    start: int
    end: int


@dataclass(frozen=True)
class WordList:
    """One old word list, ordered by block appearance then within-block start."""

    wordlist_id: str
    headwords: tuple[Headword, ...]
    matrix: TypeMatrix

    def tokens(self) -> tuple[tuple[str, ...], ...]:
        return tuple(h.tokens for h in self.headwords)

    def ids(self) -> tuple[str, ...]:
        return tuple(h.entry_id for h in self.headwords)

    def glyphs_used(self) -> frozenset[str]:
        return frozenset(t for h in self.headwords for t in h.tokens)


def _check_interval(mark: HeadwordMark, text: str) -> str:
    if mark.start < 0 or mark.end > len(text) or mark.start >= mark.end:
        raise IntervalOutOfBounds(
            f"headword {mark.entry_id!r} interval [{mark.start},{mark.end}) does not "
            f"fit block {mark.translation_id!r} of length {len(text)}",
            detail={
                "entry_id": mark.entry_id,
                "block_id": mark.translation_id,
                "start": mark.start,
                "end": mark.end,
                "text_length": len(text),
            },
        )
    return text[mark.start : mark.end]


def _check_revision(
    mark: HeadwordMark,
    block: DeliveryBlock,
    expected_revisions: Optional[Mapping[str, int]],
) -> int:
    block_rev = block.source_revisions.get(mark.segment_id)
    if block_rev is None:
        raise MalformedMark(
            f"headword {mark.entry_id!r} names segment {mark.segment_id!r} that is "
            f"not part of block {mark.translation_id!r}",
            detail={
                "entry_id": mark.entry_id,
                "block_id": block.translation_id,
                "segment_id": mark.segment_id,
                "block_segments": list(block.source_ids),
            },
        )
    if mark.source_revision is not None and int(mark.source_revision) != block_rev:
        raise StalePackage(
            f"block {mark.translation_id!r} was frozen at {mark.segment_id}"
            f"@{block_rev} but headword {mark.entry_id!r} claims "
            f"{mark.segment_id}@{mark.source_revision}: an old block pasted onto a "
            "different edition",
            detail={
                "entry_id": mark.entry_id,
                "block_id": block.translation_id,
                "segment_id": mark.segment_id,
                "block_revision": block_rev,
                "mark_revision": int(mark.source_revision),
            },
        )
    if expected_revisions is not None and mark.segment_id in expected_revisions:
        want = int(expected_revisions[mark.segment_id])
        if block_rev != want:
            raise StalePackage(
                f"block {mark.translation_id!r} carries {mark.segment_id}@{block_rev} "
                f"but the target edition expects {mark.segment_id}@{want}: an old "
                "block pasted onto a newer edition",
                detail={
                    "entry_id": mark.entry_id,
                    "block_id": block.translation_id,
                    "segment_id": mark.segment_id,
                    "block_revision": block_rev,
                    "expected_revision": want,
                },
            )
    return block_rev


def build_wordlist(
    blocks: Sequence[DeliveryBlock],
    marks: Iterable[HeadwordMark],
    matrix: TypeMatrix,
    *,
    wordlist_id: str,
    expected_revisions: Optional[Mapping[str, int]] = None,
) -> WordList:
    """Assemble one ordered old word list from delivered blocks + marks.

    Blocks appear in the order they were delivered (``position``); inside a
    block, marks are ordered by interval ``start``.  Overlapping marks and
    duplicate entry numbers are rejected.
    """

    block_by_id: dict[str, DeliveryBlock] = {b.translation_id: b for b in blocks}
    marks_by_block: dict[str, list[HeadwordMark]] = {}
    seen_entry_ids: set[str] = set()
    for mark in marks:
        if mark.entry_id in seen_entry_ids:
            raise MalformedMark(
                f"duplicate headword number {mark.entry_id!r} in word list "
                f"{wordlist_id!r}",
                detail={"entry_id": mark.entry_id, "wordlist_id": wordlist_id},
            )
        seen_entry_ids.add(mark.entry_id)
        if mark.translation_id not in block_by_id:
            raise UnknownBlock(
                f"headword {mark.entry_id!r} names block {mark.translation_id!r} "
                "that is not in the delivered package",
                detail={
                    "entry_id": mark.entry_id,
                    "block_id": mark.translation_id,
                    "available_blocks": [b.translation_id for b in blocks],
                },
            )
        marks_by_block.setdefault(mark.translation_id, []).append(mark)

    # Validate intervals within a block don't overlap, then order by start.
    for tid, group in marks_by_block.items():
        group.sort(key=lambda m: (m.start, m.end, entry_id_key(m.entry_id)))
        prev_end = -1
        prev_id: Optional[str] = None
        for mark in group:
            if mark.start < prev_end:
                raise MalformedMark(
                    f"headwords {prev_id!r} and {mark.entry_id!r} overlap inside "
                    f"block {tid!r}",
                    detail={
                        "block_id": tid,
                        "first": prev_id,
                        "second": mark.entry_id,
                    },
                )
            prev_end = mark.end
            prev_id = mark.entry_id

    headwords: list[Headword] = []
    for block in sorted(blocks, key=lambda b: b.position):
        group = marks_by_block.get(block.translation_id)
        if not group:
            continue
        for mark in group:
            substring = _check_interval(mark, block.text)
            revision = _check_revision(mark, block, expected_revisions)
            tokens = matrix.tokenize(substring)
            headwords.append(
                Headword(
                    entry_id=mark.entry_id,
                    text=substring,
                    tokens=tokens,
                    block_id=block.translation_id,
                    segment_id=mark.segment_id,
                    revision=revision,
                    start=mark.start,
                    end=mark.end,
                )
            )
    if not headwords:
        raise MalformedMark(
            f"word list {wordlist_id!r} has no headwords", detail={"wordlist_id": wordlist_id}
        )
    return WordList(wordlist_id=wordlist_id, headwords=tuple(headwords), matrix=matrix)


def build_headwords(
    blocks: Sequence[DeliveryBlock],
    marks: Iterable[HeadwordMark],
    matrix: TypeMatrix,
    *,
    label: str = "book",
    expected_revisions: Optional[Mapping[str, int]] = None,
) -> tuple[Headword, ...]:
    """Assemble an *unordered* set of real headwords (new-book entries).

    Same validation as :func:`build_wordlist`, but the caller (the book) supplies
    order via the glyph order, so block appearance order is irrelevant here.
    Duplicate entry numbers are still rejected.
    """

    wordlist = build_wordlist(
        blocks,
        marks,
        matrix,
        wordlist_id=label,
        expected_revisions=expected_revisions,
    )
    return wordlist.headwords

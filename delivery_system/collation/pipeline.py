"""Bridge: real delivered packages + a proofreading spec -> collation objects.

A scenario is plain JSON describing what the cataloguer knows:

* ``matrix``: the local type matrix (single sorts and ligatures);
* ``packages``: an ordered list of package specs (each an actual delivered
  ``translation-package/1`` content dict, or a repo ``package_id`` to load);
* ``wordlists``: old word lists, each listing marks whose ``translation_id``
  names a delivered block ``(package, chunk)`` plus an exact character interval
  and the source revision it was proofread against;
* ``book``: the new book -- its own marks over delivered blocks, the fascicle
  capacity, and the queried entries / loose-page clues.

Every headword is therefore read out of real delivered text; the pipeline never
invents vocabulary.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from ..errors import MalformedMark
from .book import (
    AdjacentClue,
    BookEntry,
    BookSolver,
    Clue,
    NewBook,
    PositionClue,
)
from .glyphs import TypeMatrix
from .handoff import (
    build_slip,
    certify_precedence,
    match_clues_against_slip,
    reconstruct_book_orders,
)
from .investigation import CollationInvestigation
from .marks import (
    DeliveryBlock,
    HeadwordMark,
    WordList,
    blocks_from_package,
    build_headwords,
    build_wordlist,
)


def _matrix(spec: Mapping[str, Any]) -> TypeMatrix:
    return TypeMatrix(tuple(str(g) for g in spec["matrix"]))


def _gather_blocks(
    package_contents: Sequence[Mapping[str, Any]]
) -> list[DeliveryBlock]:
    """Concatenate each package's blocks in (package order, chunk order).

    A ``translation_id`` names one delivered translation block; if it appears in
    more than one package (e.g. the same block re-delivered), it is counted once
    at its first appearance, preserving delivery order.
    """

    gathered: list[DeliveryBlock] = []
    seen: set[str] = set()
    for content in package_contents:
        blocks = blocks_from_package(content)
        for block in blocks:
            if block.translation_id in seen:
                continue
            seen.add(block.translation_id)
            gathered.append(
                DeliveryBlock(
                    translation_id=block.translation_id,
                    text=block.text,
                    source_ids=block.source_ids,
                    source_revisions=block.source_revisions,
                    position=len(gathered),
                )
            )
    return gathered


def _marks_from(spec_marks: Iterable[Mapping[str, Any]]) -> list[HeadwordMark]:
    marks: list[HeadwordMark] = []
    for item in spec_marks:
        marks.append(
            HeadwordMark(
                entry_id=str(item["entry_id"]),
                translation_id=str(item["translation_id"]),
                start=int(item["start"]),
                end=int(item["end"]),
                segment_id=str(item["segment_id"]),
                source_revision=None
                if item.get("source_revision") is None
                else int(item["source_revision"]),
            )
        )
    return marks


def build_investigation(
    spec: Mapping[str, Any],
    package_contents: Sequence[Mapping[str, Any]],
    *,
    expected_revisions: Optional[Mapping[str, int]] = None,
) -> tuple[CollationInvestigation, dict[str, WordList]]:
    matrix = _matrix(spec)
    blocks = _gather_blocks(package_contents)
    investigation = CollationInvestigation(matrix)
    wordlists: dict[str, WordList] = {}
    for wl_spec in spec.get("wordlists", []):
        wl = build_wordlist(
            blocks,
            _marks_from(wl_spec["marks"]),
            matrix,
            wordlist_id=str(wl_spec["wordlist_id"]),
            expected_revisions=expected_revisions,
        )
        wordlists[wl.wordlist_id] = wl
        investigation.add_wordlist(wl)
    for con in spec.get("constraints", []):
        investigation.add_constraint(
            str(con["earlier"]), str(con["later"]), source=str(con.get("source", "explicit"))
        )
    return investigation, wordlists


def build_book(
    spec: Mapping[str, Any],
    package_contents: Sequence[Mapping[str, Any]],
    *,
    expected_revisions: Optional[Mapping[str, int]] = None,
) -> NewBook:
    matrix = _matrix(spec)
    blocks = _gather_blocks(package_contents)
    book_spec = spec["book"]
    headwords = build_headwords(
        blocks,
        _marks_from(book_spec["marks"]),
        matrix,
        label=str(book_spec.get("book_id", "book")),
        expected_revisions=expected_revisions,
    )
    entries = tuple(BookEntry.from_headword(hw) for hw in headwords)
    return NewBook(
        book_id=str(book_spec.get("book_id", "book")),
        entries=entries,
        fascicle_capacity=int(book_spec["fascicle_capacity"]),
        matrix=matrix,
    )


def clues_from_spec(spec: Mapping[str, Any]) -> list[Clue]:
    clues: list[Clue] = []
    for item in spec.get("clues", []):
        kind = item.get("kind")
        if kind == "position":
            clues.append(
                PositionClue(
                    entry_id=str(item["entry_id"]),
                    fascicle=int(item["fascicle"]),
                    position=int(item["position"]),
                )
            )
        elif kind == "adjacent":
            clues.append(
                AdjacentClue(
                    before_entry_id=str(item["before_entry_id"]),
                    after_entry_id=str(item["after_entry_id"]),
                    fascicle=None if item.get("fascicle") is None else int(item["fascicle"]),
                )
            )
        else:
            raise MalformedMark(f"unknown clue kind {kind!r}")
    return clues


def run_scenario(
    spec: Mapping[str, Any],
    package_contents: Sequence[Mapping[str, Any]],
    *,
    limit: int = 20000,
) -> dict[str, Any]:
    """Run a whole collation scenario and return a JSON-able report.

    Combines: investigation (constraint orders / check), the binding query for
    each requested entry, a typesetting slip, and an optional precedence
    certificate request -- all over the same delivered packages.
    """

    expected = spec.get("expected_revisions")
    investigation, wordlists = build_investigation(
        spec, package_contents, expected_revisions=expected
    )
    inv_result = investigation.solve(limit=limit)
    report: dict[str, Any] = {
        "investigation": inv_result.to_dict(),
        "wordlists": {
            wl_id: [h.entry_id for h in wl.headwords]
            for wl_id, wl in wordlists.items()
        },
    }

    if "check" in spec:
        check = investigation.check(spec["check"])
        report["check"] = check.to_dict()

    if "book" in spec:
        book = build_book(spec, package_contents, expected_revisions=expected)
        clues = clues_from_spec(spec)
        solver = BookSolver(book, investigation, limit=limit)

        queries: dict[str, Any] = {}
        for entry_id in spec["book"].get("queries", []):
            queries[entry_id] = solver.query(entry_id, clues).to_dict()
        report["queries"] = queries

        if spec.get("handoff") in ("slip", "both"):
            slip = build_slip(book, investigation, clues, limit=limit)
            report["slip"] = slip.to_dict()
            report["slip_reconstructed_orders"] = reconstruct_book_orders(slip)
            if clues:
                report["slip_match"] = match_clues_against_slip(slip, clues)
        if spec.get("handoff") in ("certificate", "both") and spec.get("requirements"):
            reqs = [(str(b), str(a)) for b, a in spec["requirements"]]
            result = certify_precedence(book, investigation, reqs, limit=limit)
            report["certificates"] = result.to_dict()
    return report

"""End-to-end collation demo over packages *actually published* by the repo.

This mirrors ``delivery_system.demo``: it builds real source documents and
translations, freezes real ``translation-package/1`` products through
:class:`~delivery_system.delivery_repository.DeliveryRepository`, then reads those
frozen packages back and runs the lead-type investigation + binding scenarios on
them.  Nothing is hand-written: every headword is an exact interval inside a
delivered block, checked against its frozen source revision.

Scenarios reproduced (matching the brief):

* old table ``a<b``  -> new-book ``c`` possible at fascicles 1, 3, 4 (never 2);
* add a second old table ``c<a`` -> ``c`` uniquely at fascicle 1;
* keep only the original table, add loose page "``b`` at fascicle 3" -> ``c``
  uniquely at fascicle 4;
* pairwise-consistent loose pages that no single book can satisfy -> a minimal
  conflicting set is reported;
* investigator->bindery typesetting slip (prefix-compressed, layout-pruned) and
  bindery->investigator feasible-order certificates;
* identical headwords order by stable entry number;
* after a source revision + republish, the *old* frozen package still rebuilds
  its original book order from its original loose pages.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..delivery_repository import DeliveryRepository
from ..package_io import write_package
from ..source_catalog import PublishedSourceReader, SourceCatalog
from .book import BookEntry, BookSolver, NewBook, PositionClue
from .glyphs import TypeMatrix
from .handoff import (
    LocalConclusion,
    build_slip,
    certify_precedence,
    match_clues_against_slip,
    reconstruct_book_orders,
    revoke_on_new_clue,
)
from .marks import HeadwordMark, build_headwords
from .pipeline import (
    _gather_blocks,
    _marks_from,
    _matrix,
    build_book,
    build_investigation,
)

# ----------------------------------------------------------------------
# fixture documents: one translation block per segment, text == headword text
# ----------------------------------------------------------------------
# (document_id, language, [(segment_id, translation_id, text)])
_DOCS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("OLD_AB", "OLD", [("O1", "AB_A", "a"), ("O2", "AB_B", "b")]),
    ("OLD_CA", "OLD", [("O3", "CA_C", "c"), ("O4", "CA_A", "a")]),
    ("OLD_ABC", "OLD", [("P1", "ABC_A", "a"), ("P2", "ABC_B", "b"), ("P3", "ABC_C", "c")]),
    ("NEW4", "NEW", [("Q1", "N4_AA", "aa"), ("Q2", "N4_AB", "ab"), ("Q3", "N4_B", "b"), ("Q4", "N4_C", "c")]),
    (
        "NEWCONF",
        "NEW",
        [
            ("R1", "NC0", "a"),
            ("R2", "NC1", "b"),
            ("R3", "NC2", "c"),
            ("R4", "NC3", "d"),
            ("R5", "NC4", "e"),
            ("R6", "NC5", "a"),
        ],
    ),
    ("OLD_DUP", "OLD", [("U1", "DP2", "a"), ("U2", "DP10", "a")]),
    # Ligature translation: 'ab' is one whole sort; 'a','b','c' are single sorts.
    (
        "LIGCONF",
        "NEW",
        [
            ("V1", "LC0", "ab"),
            ("V2", "LC1", "a"),
            ("V3", "LC2", "b"),
            ("V4", "LC3", "c"),
            ("V5", "LC4", "ab"),
        ],
    ),
]

# package_id -> document_id  (published after translations are loaded)
_PACKAGES: dict[str, str] = {
    "PKG_OLD_AB": "OLD_AB",
    "PKG_OLD_CA": "OLD_CA",
    "PKG_OLD_ABC": "OLD_ABC",
    "PKG_NEW4": "NEW4",
    "PKG_NEWCONF": "NEWCONF",
    "PKG_OLD_DUP": "OLD_DUP",
    "PKG_LIGCONF": "LIGCONF",
}


def _publish_all(out_dir: str) -> tuple[dict[str, dict[str, Any]], str, str, DeliveryRepository, SourceCatalog]:
    os.makedirs(out_dir, exist_ok=True)
    source_db = os.path.join(out_dir, "source.db")
    delivery_db = os.path.join(out_dir, "delivery.db")
    packages_dir = os.path.join(out_dir, "packages")
    os.makedirs(packages_dir, exist_ok=True)

    catalog = SourceCatalog(source_db)
    repo = DeliveryRepository(delivery_db, PublishedSourceReader(source_db))

    for doc_id, language, rows in _DOCS:
        catalog.create_document(
            doc_id, [{"segment_id": sid, "text": text} for sid, _tid, text in rows]
        )
        for sid, tid, text in rows:
            repo.add_translation(
                translation_id=tid,
                document_id=doc_id,
                language=language,
                text=text,
                source_refs=[{"segment_id": sid, "revision": 1}],
            )
    packages: dict[str, dict[str, Any]] = {}
    for pkg_id, doc_id in _PACKAGES.items():
        language = next(lang for d, lang, _ in _DOCS if d == doc_id)
        content = repo.publish_language(doc_id, language, pkg_id)
        write_package(content, os.path.join(packages_dir, f"{pkg_id}.json"))
        packages[pkg_id] = content
    return packages, source_db, delivery_db, repo, catalog


# ----------------------------------------------------------------------
# helpers to build marks/books from a spec
# ----------------------------------------------------------------------
def _headword_marks(spec_marks) -> list[HeadwordMark]:
    return _marks_from(spec_marks)


def _build_from_spec(spec, packages):
    contents = [packages[p] for p in spec["packages"]]
    matrix = _matrix(spec)
    inv, wordlists = build_investigation(spec, contents)
    result = inv.solve()
    report: dict[str, Any] = {
        "investigation": result.to_dict(),
        "wordlists": {k: list(v.ids()) for k, v in wordlists.items()},
    }
    if "check" in spec:
        report["check"] = inv.check(spec["check"]).to_dict()
    clues: list = []
    from .pipeline import clues_from_spec

    clues = clues_from_spec(spec)
    book = None
    solver = None
    if "book" in spec:
        book = build_book(spec, contents)
        solver = BookSolver(book, inv)
        query_results = {}
        for eid in spec["book"].get("queries", []):
            query_results[eid] = solver.query(eid, clues).to_dict()
        report["queries"] = query_results
    if spec.get("handoff") in ("slip", "both") and book is not None:
        slip = build_slip(book, inv, clues)
        report["slip"] = {
            "distinct_book_orders": slip.distinct_book_orders,
            "pruned_layout_duplicates": slip.pruned_layout_duplicates,
            "surviving_orders": slip.surviving_orders,
        }
        report["slip_orders"] = [
            {"book_order": o["book_order"], "witness_order": o["witness_order"]}
            for o in reconstruct_book_orders(slip)
        ]
        if clues:
            report["slip_bindery_match"] = [
                o["book_order"] for o in match_clues_against_slip(slip, clues)
            ]
    if spec.get("handoff") in ("certificate", "both") and book is not None:
        reqs = [(str(b), str(a)) for b, a in spec.get("requirements", [])]
        report["certificates"] = certify_precedence(book, inv, reqs).to_dict()
    return report, inv, book, solver, clues


def run_collation_demo(out_dir: str) -> dict[str, Any]:
    packages, source_db, delivery_db, repo, catalog = _publish_all(out_dir)
    report: dict[str, Any] = {"ok": True, "checks": [], "scenarios": {}}

    def check(name: str, cond: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            report["ok"] = False

    # ---- Scenario 1: old table a<b ; c at 1,3,4 (never 2) ----
    s1 = {
        "matrix": ["a", "b", "c"],
        "packages": ["PKG_OLD_AB", "PKG_NEW4"],
        "wordlists": [
            {
                "wordlist_id": "old_ab",
                "marks": [
                    {"entry_id": "1", "translation_id": "AB_A", "start": 0, "end": 1, "segment_id": "O1", "source_revision": 1},
                    {"entry_id": "2", "translation_id": "AB_B", "start": 0, "end": 1, "segment_id": "O2", "source_revision": 1},
                ],
            }
        ],
        "book": {
            "book_id": "N4",
            "fascicle_capacity": 1,
            "marks": [
                {"entry_id": "1", "translation_id": "N4_AA", "start": 0, "end": 2, "segment_id": "Q1", "source_revision": 1},
                {"entry_id": "2", "translation_id": "N4_AB", "start": 0, "end": 2, "segment_id": "Q2", "source_revision": 1},
                {"entry_id": "3", "translation_id": "N4_B", "start": 0, "end": 1, "segment_id": "Q3", "source_revision": 1},
                {"entry_id": "4", "translation_id": "N4_C", "start": 0, "end": 1, "segment_id": "Q4", "source_revision": 1},
            ],
            "queries": ["4"],
        },
        "handoff": "slip",
    }
    r1, _inv, _b, _s, _c = _build_from_spec(s1, packages)
    report["scenarios"]["single_old_table"] = r1
    cpos = sorted(p["fascicle"] for p in r1["queries"]["4"]["possible_positions"])
    check("single_old_c_at_1_3_4", cpos == [1, 3, 4], cpos)
    check("single_old_c_never_2", 2 not in cpos, cpos)
    check("single_old_c_not_unique", not r1["queries"]["4"]["unique"])

    # ---- Scenario 2: add second old table c<a ; c uniquely at 1 ----
    s2 = json.loads(json.dumps(s1))
    s2["packages"] = ["PKG_OLD_AB", "PKG_OLD_CA", "PKG_NEW4"]
    s2["wordlists"].append(
        {
            "wordlist_id": "old_ca",
            "marks": [
                {"entry_id": "3", "translation_id": "CA_C", "start": 0, "end": 1, "segment_id": "O3", "source_revision": 1},
                {"entry_id": "4", "translation_id": "CA_A", "start": 0, "end": 1, "segment_id": "O4", "source_revision": 1},
            ],
        }
    )
    r2, *_ = _build_from_spec(s2, packages)
    report["scenarios"]["two_old_tables"] = r2
    check("two_tables_c_unique_at_1", r2["queries"]["4"]["unique"] and r2["queries"]["4"]["signed_position"] == [1, 1],
          r2["queries"]["4"]["signed_position"])

    # ---- Scenario 3: keep old table, loose page "b fas 3" -> c fas 4 ----
    s3 = json.loads(json.dumps(s1))
    s3["clues"] = [{"kind": "position", "entry_id": "3", "fascicle": 3, "position": 1}]
    r3, *_ = _build_from_spec(s3, packages)
    report["scenarios"]["loose_page_b3"] = r3
    check("loose_b3_c_unique_at_4", r3["queries"]["4"]["unique"] and r3["queries"]["4"]["signed_position"] == [4, 1],
          r3["queries"]["4"]["signed_position"])

    # ---- Scenario 4: pairwise-consistent but jointly impossible pages ----
    s4 = {
        "matrix": ["a", "b", "c", "d", "e"],
        "packages": ["PKG_OLD_ABC", "PKG_NEWCONF"],
        "wordlists": [
            {
                "wordlist_id": "old_abc",
                "marks": [
                    {"entry_id": "1", "translation_id": "ABC_A", "start": 0, "end": 1, "segment_id": "P1", "source_revision": 1},
                    {"entry_id": "2", "translation_id": "ABC_B", "start": 0, "end": 1, "segment_id": "P2", "source_revision": 1},
                    {"entry_id": "3", "translation_id": "ABC_C", "start": 0, "end": 1, "segment_id": "P3", "source_revision": 1},
                ],
            }
        ],
        "book": {
            "book_id": "NEWCONF",
            "fascicle_capacity": 2,
            "marks": [
                {"entry_id": "N0", "translation_id": "NC0", "start": 0, "end": 1, "segment_id": "R1", "source_revision": 1},
                {"entry_id": "N1", "translation_id": "NC1", "start": 0, "end": 1, "segment_id": "R2", "source_revision": 1},
                {"entry_id": "N2", "translation_id": "NC2", "start": 0, "end": 1, "segment_id": "R3", "source_revision": 1},
                {"entry_id": "N3", "translation_id": "NC3", "start": 0, "end": 1, "segment_id": "R4", "source_revision": 1},
                {"entry_id": "N4", "translation_id": "NC4", "start": 0, "end": 1, "segment_id": "R5", "source_revision": 1},
                {"entry_id": "N5", "translation_id": "NC5", "start": 0, "end": 1, "segment_id": "R6", "source_revision": 1},
            ],
            "queries": ["N0"],
        },
        "clues": [
            {"kind": "position", "entry_id": "N0", "fascicle": 1, "position": 2},
            {"kind": "position", "entry_id": "N2", "fascicle": 3, "position": 1},
            {"kind": "position", "entry_id": "N3", "fascicle": 2, "position": 2},
        ],
    }
    r4, inv4, book4, solver4, clues4 = _build_from_spec(s4, packages)
    report["scenarios"]["joint_conflict"] = {
        "query": r4["queries"]["N0"],
        "pairwise": {},
    }
    # Confirm every pair is individually satisfiable but the triple is not.
    pair_ok = True
    for i in range(3):
        for j in range(i + 1, 3):
            pair = [clues4[i], clues4[j]]
            pair_ok = pair_ok and bool(solver4.surviving_layouts(pair))
    check("conflict_pairs_individually_ok", pair_ok)
    check("conflict_triple_unsatisfiable", not r4["queries"]["N0"]["satisfiable"])
    report["scenarios"]["joint_conflict"]["pairwise"]["all_pairs_ok"] = pair_ok

    # ---- Scenario 4b: same two properties with a *ligature* translation ----
    # Type matrix {a, b, c, 'ab'}; entries E0..E4 = ab, a, b, c, ab.  The two
    # 'ab' entries are identical headwords and must keep their entry-number
    # order; and three loose pages are pairwise-consistent yet jointly
    # impossible.
    s4b = {
        "matrix": ["a", "b", "c", "ab"],
        "packages": ["PKG_OLD_ABC", "PKG_LIGCONF"],
        "wordlists": [
            {
                "wordlist_id": "old_a_b",
                "marks": [
                    {"entry_id": "1", "translation_id": "ABC_A", "start": 0, "end": 1, "segment_id": "P1", "source_revision": 1},
                    {"entry_id": "2", "translation_id": "ABC_B", "start": 0, "end": 1, "segment_id": "P2", "source_revision": 1},
                ],
            }
        ],
        "book": {
            "book_id": "LIGCONF",
            "fascicle_capacity": 2,
            "marks": [
                {"entry_id": "E0", "translation_id": "LC0", "start": 0, "end": 2, "segment_id": "V1", "source_revision": 1},
                {"entry_id": "E1", "translation_id": "LC1", "start": 0, "end": 1, "segment_id": "V2", "source_revision": 1},
                {"entry_id": "E2", "translation_id": "LC2", "start": 0, "end": 1, "segment_id": "V3", "source_revision": 1},
                {"entry_id": "E3", "translation_id": "LC3", "start": 0, "end": 1, "segment_id": "V4", "source_revision": 1},
                {"entry_id": "E4", "translation_id": "LC4", "start": 0, "end": 2, "segment_id": "V5", "source_revision": 1},
            ],
            "queries": ["E0"],
        },
        "clues": [
            {"kind": "position", "entry_id": "E0", "fascicle": 1, "position": 1},
            {"kind": "position", "entry_id": "E3", "fascicle": 2, "position": 1},
            {"kind": "adjacent", "before_entry_id": "E1", "after_entry_id": "E2"},
        ],
    }
    r4b, inv4b, book4b, solver4b, clues4b = _build_from_spec(s4b, packages)
    lig_pair_ok = True
    for i in range(3):
        for j in range(i + 1, 3):
            lig_pair_ok = lig_pair_ok and bool(
                solver4b.surviving_layouts([clues4b[i], clues4b[j]])
            )
    check("ligature_conflict_pairs_individually_ok", lig_pair_ok)
    check("ligature_conflict_triple_unsatisfiable", not r4b["queries"]["E0"]["satisfiable"])
    check("ligature_conflict_kind_loose_pages", r4b["queries"]["E0"]["conflict"]["kind"] == "loose_pages")
    # Identical 'ab' headwords E0 and E4 keep entry-number order in every book.
    lig_consistent = True
    for _order, ordered, _layout in solver4b._layout_cache():
        if ordered.index("E0") > ordered.index("E4"):
            lig_consistent = False
    check("ligature_identical_headwords_fixed_number_order", lig_consistent)
    report["scenarios"]["ligature"] = {
        "query": r4b["queries"]["E0"],
        "pairs_individually_ok": lig_pair_ok,
        "identical_order_fixed": lig_consistent,
    }

    # ---- Scenario 5: handoff certificate (c before b) ----
    s5 = json.loads(json.dumps(s1))
    s5["handoff"] = "certificate"
    s5["requirements"] = [["4", "3"]]  # c before b
    r5, *_ = _build_from_spec(s5, packages)
    report["scenarios"]["certificate"] = r5["certificates"]
    check("certificate_feasible", r5["certificates"]["satisfiable"])
    check(
        "certificate_only_feasible_orders",
        all(
            list(c["glyph_order"]).index("a") < list(c["glyph_order"]).index("b")
            and list(c["glyph_order"]).index("c") < list(c["glyph_order"]).index("b")
            for c in r5["certificates"]["certificates"]
        ),
    )

    # ---- Scenario 6: identical headwords order by entry number ----
    s6_good = {
        "matrix": ["a", "b"],
        "packages": ["PKG_OLD_DUP"],
        "wordlists": [
            {
                "wordlist_id": "dup_good",
                "marks": [
                    {"entry_id": "2", "translation_id": "DP2", "start": 0, "end": 1, "segment_id": "U1", "source_revision": 1},
                    {"entry_id": "10", "translation_id": "DP10", "start": 0, "end": 1, "segment_id": "U2", "source_revision": 1},
                ],
            }
        ],
    }
    r6, *_ = _build_from_spec(s6_good, packages)
    report["scenarios"]["identical_good"] = r6["investigation"]["feasible"]
    check("identical_entry_order_ok", r6["investigation"]["feasible"])

    s6_bad = json.loads(json.dumps(s6_good))
    s6_bad["wordlists"][0]["marks"][0]["entry_id"] = "10"
    s6_bad["wordlists"][0]["marks"][1]["entry_id"] = "2"
    r6b, *_ = _build_from_spec(s6_bad, packages)
    check(
        "identical_wrong_order_rejected",
        not r6b["investigation"]["feasible"]
        and any(c["kind"] == "entry_number" for c in r6b["investigation"]["contradictions"]),
        r6b["investigation"]["contradictions"],
    )

    # ---- Scenario 7: revocation of a local conclusion by a new page ----
    # Two independent local claims about where entry 3 ('b') sits, each read
    # from its own loose page. They cannot both be true. A newly found page
    # saying b is at fascicle 3 overturns exactly the b@fascicle-2 claim.
    b_at_2 = LocalConclusion(
        key="b@2", kind="position", entry_id="3", target=(2, 1),
        depended_on=(PositionClue("3", 2, 1).to_dict(),),
        _clues=(PositionClue("3", 2, 1),),
    )
    b_at_3 = LocalConclusion(
        key="b@3", kind="position", entry_id="3", target=(3, 1),
        depended_on=(PositionClue("3", 3, 1).to_dict(),),
        _clues=(PositionClue("3", 3, 1),),
    )
    # Use the scenario-1 investigation/book (a<b old table, N4 book).
    _, inv1, book1, _s1, _c1 = _build_from_spec(s1, packages)
    rev = revoke_on_new_clue(
        book1, inv1, [b_at_2, b_at_3], PositionClue("3", 3, 1)
    )
    report["scenarios"]["revocation"] = rev.to_dict()
    check(
        "revocation_keeps_supported_claim",
        {c["key"] for c in rev.to_dict()["retained"]} == {"b@3"},
        rev.to_dict(),
    )
    check(
        "revocation_drops_overturned_claim",
        {c["key"] for c in rev.to_dict()["revoked"]} == {"b@2"},
        rev.to_dict(),
    )

    # ---- Scenario 8: old package still rebuilds its book order after revision ----
    # Revise N4_AB's segment (Q2 -> rev2), republish NEW4 as _V2, then read the
    # OLD frozen NEW4 package: its loose pages still restore the original order.
    new_trans = repo.add_translation(
        "N4_AB_v2", "NEW4", "NEW", "ab", [{"segment_id": "Q2", "revision": 2}]
    )
    catalog.revise_segment("NEW4", "Q2", "ab (revised)", revision=2)
    new4_v2 = repo.publish_language("NEW4", "NEW", "PKG_NEW4_V2")
    old_pkg = packages["PKG_NEW4"]
    old_revs = {s["segment_id"]: s["revision"] for s in old_pkg["source_snapshot"]}
    check("old_package_keeps_rev1", all(r == 1 for r in old_revs.values()), old_revs)
    check("new_package_has_rev2", any(s["revision"] == 2 for s in new4_v2["source_snapshot"]))

    m = TypeMatrix(("a", "b", "c"))
    old_blocks = _gather_blocks([old_pkg])
    old_marks = [
        HeadwordMark("1", "N4_AA", 0, 2, "Q1", 1),
        HeadwordMark("2", "N4_AB", 0, 2, "Q2", 1),
        HeadwordMark("3", "N4_B", 0, 1, "Q3", 1),
        HeadwordMark("4", "N4_C", 0, 1, "Q4", 1),
    ]
    # expected_revisions pins the old edition; the old package satisfies it.
    old_book = NewBook(
        "N4_old",
        tuple(BookEntry.from_headword(h) for h in build_headwords(old_blocks, old_marks, m)),
        1,
        m,
    )
    inv_old = build_investigation(s1, [packages["PKG_OLD_AB"], old_pkg])[0]
    old_solver = BookSolver(old_book, inv_old)
    old_q = old_solver.query("4", [PositionClue("3", 3, 1)])
    check(
        "old_revision_restores_book_order",
        old_q.unique and old_q.signed_position == (4, 1),
        old_q.signed_position,
    )
    # The old marks (Q2@rev1) must be rejected against the *new* (rev2) package:
    # blindly pasting an old block onto a newer edition is exactly what we refuse.
    v2_marks = [
        HeadwordMark("1", "N4_AA", 0, 2, "Q1", 1),
        HeadwordMark("2", "N4_AB_v2", 0, 2, "Q2", 1),  # old revision claim vs rev2 block
        HeadwordMark("3", "N4_B", 0, 1, "Q3", 1),
        HeadwordMark("4", "N4_C", 0, 1, "Q4", 1),
    ]
    try:
        build_headwords(
            _gather_blocks([new4_v2]), v2_marks, m, expected_revisions={"Q2": 1}
        )
        stale_rejected = False
    except Exception as exc:  # StalePackage
        stale_rejected = type(exc).__name__ == "StalePackage"
    check("new_package_is_stale_against_old_marks", stale_rejected)

    report["document_id"] = "collation-demo"
    return report

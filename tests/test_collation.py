"""Tests for the lead-type character-order investigation + binding subsystem.

Run with ``python -m unittest discover -s tests -v``.

Two layers:

* **Unit/API tests** exercise tokenisation, comparison, constraint solving,
  checking, book queries, slips and certificates with small in-memory fixtures.
* **Integration tests** build word lists / books out of *real*
  ``translation-package/1`` content dicts (the same shape the delivery side
  freezes), reading headwords from exact character intervals and source
  revisions -- exercising the reject-stale / reject-untokenizable / reject-
  out-of-bounds rules.
"""

from __future__ import annotations

import json
import unittest

from delivery_system.collation import (
    AdjacentClue,
    BookEntry,
    BookSolver,
    CollationInvestigation,
    CollationSlip,
    LocalConclusion,
    PositionClue,
    TypeMatrix,
    WordList,
    HeadwordMark,
    build_headwords,
    build_slip,
    build_wordlist,
    blocks_from_package,
    certify_precedence,
    compare_headwords,
    entry_id_key,
    match_clues_against_slip,
    reconstruct_book_orders,
    revoke_on_new_clue,
    viable_conclusions,
)
from delivery_system.collation.pipeline import run_scenario
from delivery_system.errors import (
    IntervalOutOfBounds,
    MalformedMark,
    StalePackage,
    TooManyOrders,
    UnknownBlock,
    UnknownEntry,
    Untokenizable,
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def matrix(*glyphs, ligatures=()):
    return TypeMatrix(tuple(glyphs) + tuple(ligatures))


def hw(eid, text, mat):
    return _HW(eid, text, mat)


class _HW:  # lightweight shim that mirrors Headword's needed fields
    def __init__(self, eid, text, mat):
        from delivery_system.collation import Headword

        self.__dict__.update(
            Headword(
                entry_id=eid,
                text=text,
                tokens=mat.tokenize(text),
                block_id="blk",
                segment_id="S1",
                revision=1,
                start=0,
                end=len(text),
            ).__dict__
        )


def wordlist(wl_id, pairs, mat):
    from delivery_system.collation import Headword

    headwords = tuple(
        Headword(
            entry_id=eid,
            text=text,
            tokens=mat.tokenize(text),
            block_id="blk",
            segment_id="S1",
            revision=1,
            start=0,
            end=len(text),
        )
        for eid, text in pairs
    )
    return WordList(wl_id, headwords, mat)


def book(entries, capacity, mat, book_id="book"):
    be = tuple(
        BookEntry(eid, mat.tokenize(text), text, "blk", "S1", 1)
        for eid, text in entries
    )
    from delivery_system.collation import NewBook

    return NewBook(book_id, be, capacity, mat)


def positions_held(layouts, pred):
    return frozenset(i for i, (_o, _b, lay) in enumerate(layouts) if pred(lay))


# ----------------------------------------------------------------------
# glyphs / tokenisation / comparison
# ----------------------------------------------------------------------
class GlyphTests(unittest.TestCase):
    def test_longest_match_ligature(self):
        m = matrix("a", "b", "c", ligatures=("ab",))
        self.assertEqual(m.tokenize("abc"), ("ab", "c"))
        self.assertEqual(m.tokenize("aab"), ("a", "ab"))

    def test_untokenizable_rejected(self):
        m = matrix("a", "b")
        with self.assertRaises(Untokenizable):
            m.tokenize("az")

    def test_empty_headword_rejected(self):
        m = matrix("a")
        with self.assertRaises(Untokenizable):
            m.tokenize("")

    def test_duplicate_sorts_rejected(self):
        with self.assertRaises(MalformedMark):
            TypeMatrix(("a", "a"))

    def test_prefix_shorter_first(self):
        rank = {"a": 0, "b": 1}
        self.assertEqual(compare_headwords(("a",), "1", ("a", "b"), "2", rank), -1)
        self.assertEqual(compare_headwords(("a", "b"), "2", ("a",), "1", rank), 1)

    def test_first_differing_sort_decides(self):
        rank = {"a": 0, "b": 1, "c": 2}
        self.assertEqual(compare_headwords(("a", "c"), "1", ("b",), "2", rank), -1)
        self.assertEqual(compare_headwords(("b", "a"), "1", ("c",), "2", rank), -1)

    def test_identical_headwords_tie_by_entry_number(self):
        rank = {"a": 0}
        self.assertEqual(compare_headwords(("a",), "10", ("a",), "9", rank), 1)
        self.assertEqual(compare_headwords(("a",), "9", ("a",), "10", rank), -1)
        # numeric, not lexical: "10" > "9"
        self.assertGreater(entry_id_key("10"), entry_id_key("9"))


# ----------------------------------------------------------------------
# investigation
# ----------------------------------------------------------------------
class InvestigationTests(unittest.TestCase):
    def test_single_wordlist_partial_order(self):
        m = matrix("a", "b", "c")
        inv = CollationInvestigation(m).add_wordlist(wordlist("old", [("1", "a"), ("2", "b")], m))
        res = inv.solve()
        self.assertEqual(res.count, 3)  # c can sit in 3 places around a<b
        self.assertTrue(res.feasible)
        self.assertTrue(all(o.order.index("a") < o.order.index("b") for o in res.orders))

    def test_constraint_is_expressed_not_permuted(self):
        m = matrix("a", "b", "c")
        inv = CollationInvestigation(m).add_wordlist(wordlist("old", [("1", "a"), ("2", "b")], m))
        res = inv.solve()
        self.assertIn((("a", "b")), {(c.earlier, c.later) for c in res.constraints})

    def test_multiple_wordlists_all_must_hold(self):
        m = matrix("a", "b", "c")
        inv = (
            CollationInvestigation(m)
            .add_wordlist(wordlist("old1", [("1", "a"), ("2", "b")], m))
            .add_wordlist(wordlist("old2", [("3", "c"), ("4", "a")], m))
        )
        res = inv.solve()
        self.assertEqual([o.order for o in res.orders], [("c", "a", "b")])

    def test_prefix_contradiction_reported(self):
        m = matrix("a", "b")
        # Longer headword "ab" listed before its prefix "a": impossible.
        inv = CollationInvestigation(m).add_wordlist(wordlist("old", [("1", "ab"), ("2", "a")], m))
        res = inv.solve()
        self.assertFalse(res.feasible)
        self.assertEqual(res.contradictions[0].kind, "prefix")
        self.assertEqual(res.contradictions[0].left_entry, "1")

    def test_entry_number_contradiction_for_identical_headwords(self):
        m = matrix("a", "b")
        inv = CollationInvestigation(m).add_wordlist(wordlist("old", [("5", "a"), ("2", "a")], m))
        res = inv.solve()
        self.assertFalse(res.feasible)
        self.assertEqual(res.contradictions[0].kind, "entry_number")

    def test_cycle_contradiction(self):
        m = matrix("a", "b", "c")
        inv = (
            CollationInvestigation(m)
            .add_wordlist(wordlist("old1", [("1", "a"), ("2", "b")], m))
            .add_wordlist(wordlist("old2", [("3", "b"), ("4", "a")], m))
        )
        res = inv.solve()
        self.assertFalse(res.feasible)
        self.assertTrue(res.cycle)

    def test_check_good_order(self):
        m = matrix("a", "b", "c")
        inv = CollationInvestigation(m).add_wordlist(wordlist("old", [("1", "a"), ("2", "b")], m))
        result = inv.check(["a", "b", "c"])
        self.assertTrue(result.ok)

    def test_check_pins_pair_and_edition(self):
        m = matrix("a", "b", "c")
        inv = CollationInvestigation(m).add_wordlist(wordlist("old", [("1", "a"), ("2", "b")], m))
        result = inv.check(["b", "a", "c"])
        self.assertFalse(result.ok)
        v = result.violations[0]
        self.assertEqual(v.kind, "glyph_order")
        self.assertEqual((v.left_entry, v.right_entry), ("1", "2"))
        # provenance ties it to the concrete block/segment/revision
        self.assertEqual(v.left_provenance["segment_id"], "S1")
        self.assertIn("revision", v.left_provenance)
        self.assertEqual(v.expected, "a before b")

    def test_check_rejects_incomplete_or_duplicate_order(self):
        m = matrix("a", "b", "c")
        inv = CollationInvestigation(m)
        with self.assertRaises(Exception):
            inv.check(["a", "b"])  # missing c
        with self.assertRaises(Exception):
            inv.check(["a", "a", "b", "c"])

    def test_limit_raises_too_many(self):
        m = matrix("a", "b", "c", "d")
        inv = CollationInvestigation(m)  # no constraints -> 24 orders
        with self.assertRaises(TooManyOrders):
            inv.solve(limit=10)


# ----------------------------------------------------------------------
# book binding (the brief's worked example)
# ----------------------------------------------------------------------
class WorkedExampleTests(unittest.TestCase):
    def setUp(self):
        self.m = matrix("a", "b", "c")
        self.old = CollationInvestigation(self.m).add_wordlist(
            wordlist("old", [("1", "a"), ("2", "b")], self.m)  # a before b
        )
        self.book = book(
            [("1", "aa"), ("2", "ab"), ("3", "b"), ("4", "c")], 1, self.m
        )

    def test_c_possible_at_1_3_4_never_2(self):
        s = BookSolver(self.book, self.old)
        q = s.query("4")
        self.assertTrue(q.satisfiable)
        self.assertEqual(
            sorted((p.fascicle, p.position) for p in q.possible_positions),
            [(1, 1), (3, 1), (4, 1)],
        )
        self.assertNotIn(2, [p.fascicle for p in q.possible_positions])
        self.assertFalse(q.unique)

    def test_each_possible_position_has_witness_order(self):
        s = BookSolver(self.book, self.old)
        q = s.query("4")
        for p in q.possible_positions:
            self.assertEqual(len(p.witness_order), 3)
            self.assertEqual(len(p.book_order), 4)
            # witness really is a full order placing c at the claimed spot
            self.assertEqual(p.book_order.index("4") // 1 + 1, p.fascicle)

    def test_add_second_old_table_makes_c_unique_at_1(self):
        inv = (
            CollationInvestigation(self.m)
            .add_wordlist(wordlist("old", [("1", "a"), ("2", "b")], self.m))
            .add_wordlist(wordlist("old2", [("3", "c"), ("4", "a")], self.m))
        )
        s = BookSolver(self.book, inv)
        q = s.query("4")
        self.assertTrue(q.unique)
        self.assertEqual(q.signed_position, (1, 1))

    def test_loose_page_b_at_fascicle_3_makes_c_unique_at_4(self):
        s = BookSolver(self.book, self.old)
        q = s.query("4", [PositionClue("3", 3, 1)])  # entry 3 ('b') in fascicle 3
        self.assertTrue(q.unique)
        self.assertEqual(q.signed_position, (4, 1))

    def test_unknown_entry_query_rejected(self):
        s = BookSolver(self.book, self.old)
        with self.assertRaises(UnknownEntry):
            s.query("ZZ")

    def test_every_witness_really_places_entry_at_claimed_position(self):
        from delivery_system.collation.book import _layout_for_order
        from delivery_system.collation import GlyphOrder

        s = BookSolver(self.book, self.old)
        q = s.query("4")
        for p in q.possible_positions:
            wo = GlyphOrder(p.witness_order)
            ordered, layout = _layout_for_order(self.book, wo)
            self.assertEqual(layout["4"], (p.fascicle, p.position))
            self.assertEqual(ordered, p.book_order)

    def test_adjacent_clue_impossible_at_capacity_1(self):
        # Capacity 1 puts every entry in its own fascicle, so no two entries can
        # ever be adjacent; any adjacency clue is unsatisfiable.
        s = BookSolver(self.book, self.old)
        q = s.query("4", [AdjacentClue("1", "2", fascicle=1)])
        self.assertFalse(q.satisfiable)
        self.assertIsNotNone(q.conflict)

    def test_adjacent_clue_capacity2(self):
        # capacity 2: 'a','b' can share a fascicle and be adjacent.
        m = matrix("a", "b", "c")
        b = book([("1", "a"), ("2", "b"), ("3", "c")], 2, m)
        inv = CollationInvestigation(m).add_wordlist(
            wordlist("old", [("1", "a"), ("2", "b")], m)
        )
        s = BookSolver(b, inv)
        # a immediately before b requires them adjacent: possible when a,b are
        # the first two in the order (positions 1,2 of fascicle 1).
        q = s.query("3", [AdjacentClue("1", "2", fascicle=1)])
        self.assertTrue(q.satisfiable)


# ----------------------------------------------------------------------
# "single pages plausible, whole volume impossible"
# ----------------------------------------------------------------------
class LoosePageConflictTests(unittest.TestCase):
    def setUp(self):
        self.m = matrix("a", "b", "c", "d", "e")
        self.inv = CollationInvestigation(self.m).add_wordlist(
            wordlist("old", [("1", "a"), ("2", "b"), ("3", "c")], self.m)
        )
        # 6 single-entry words over 5 glyphs, 2 per fascicle -> 20 glyph orders.
        words = ["a", "b", "c", "d", "e", "a"]
        self.book = book([(f"N{i}", w) for i, w in enumerate(words)], 2, self.m)
        self.solver = BookSolver(self.book, self.inv)

    def test_pairwise_consistent_triple_is_jointly_impossible(self):
        clues = [
            PositionClue("N0", 1, 2),
            PositionClue("N2", 3, 1),
            PositionClue("N3", 2, 2),
        ]
        # every pair is individually satisfiable ...
        for i in range(3):
            for j in range(i + 1, 3):
                pair = [clues[i], clues[j]]
                self.assertTrue(
                    self.solver.surviving_layouts(pair), (i, j)
                )
        # ... but all three together are not.
        self.assertEqual(self.solver.surviving_layouts(clues), [])
        q = self.solver.query("N0", clues)
        self.assertFalse(q.satisfiable)
        self.assertIsNotNone(q.conflict)
        self.assertEqual(q.conflict.kind, "loose_pages")
        # The explanation is a small conflicting subset (not the whole pile).
        self.assertLessEqual(len(q.conflict.conflicting_clues), 3)

    def test_single_clue_alone_is_not_conflicting(self):
        clues = [PositionClue("N0", 1, 2)]
        q = self.solver.query("N0", clues)
        self.assertTrue(q.satisfiable)


# ----------------------------------------------------------------------
# same-headword fixed order
# ----------------------------------------------------------------------
class IdenticalHeadwordTests(unittest.TestCase):
    def test_identical_headwords_sort_by_entry_number(self):
        m = matrix("a", "b")
        inv = CollationInvestigation(m).add_wordlist(
            wordlist("old", [("2", "a"), ("10", "a")], m)  # 2 before 10
        )
        res = inv.solve()
        self.assertTrue(res.feasible)  # 2 then 10 is numeric order: fine
        inv_bad = CollationInvestigation(m).add_wordlist(
            wordlist("old", [("10", "a"), ("2", "a")], m)  # wrong numeric order
        )
        self.assertFalse(inv_bad.solve().feasible)

    def test_identical_headwords_in_book_follow_number(self):
        # Book with two identical headwords: 9 then 10, capacity 1.
        m = matrix("a")
        b = book([("10", "a"), ("9", "a")], 1, m)
        inv = CollationInvestigation(m)
        s = BookSolver(b, inv)
        q = s.query("9")
        # regardless of glyph order (only 'a'), 9 must come before 10.
        for p in q.possible_positions:
            self.assertEqual(p.book_order, ("9", "10"))


# ----------------------------------------------------------------------
# hand-off: slip and certificates
# ----------------------------------------------------------------------
class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.m = matrix("a", "b", "c")
        self.inv = CollationInvestigation(self.m).add_wordlist(
            wordlist("old", [("1", "a"), ("2", "b")], self.m)
        )
        self.book = book([("1", "aa"), ("2", "ab"), ("3", "b"), ("4", "c")], 1, self.m)

    def test_slip_roundtrips_to_book_orders(self):
        slip = build_slip(self.book, self.inv)
        self.assertIsInstance(slip, CollationSlip)
        orders = reconstruct_book_orders(slip)
        self.assertEqual(len(orders), slip.distinct_book_orders)
        self.assertEqual(
            {tuple(o["book_order"]) for o in orders},
            {("1", "2", "3", "4"), ("1", "2", "4", "3"), ("4", "1", "2", "3")},
        )
        # every reconstructed order carries a witness glyph order + layout
        for o in orders:
            self.assertEqual(len(o["witness_order"]), 3)
            self.assertEqual(tuple(o["layout"]["4"]), (o["book_order"].index("4") + 1, 1))

    def test_slip_prunes_layout_identical_orders(self):
        # Two glyph orders that yield the same book order must collapse.
        m = matrix("a", "b", "c")
        # 'b' alone and 'c' alone both appear once; force a tie by one entry.
        b = book([("1", "a"), ("2", "b")], 5, m)
        inv = CollationInvestigation(m)  # no constraints -> 6 glyph orders
        slip = build_slip(b, inv)
        # distinct layouts <= glyph orders; both entries in one fascicle always
        self.assertLessEqual(slip.distinct_book_orders, slip.surviving_orders)

    def test_slip_json_roundtrip_and_bindery_match(self):
        slip = build_slip(self.book, self.inv)
        data = json.loads(json.dumps(slip.to_dict()))
        matched = match_clues_against_slip(data, [PositionClue("3", 3, 1)])
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["book_order"], ["1", "2", "3", "4"])

    def test_certificate_returns_only_feasible_orders(self):
        res = certify_precedence(self.book, self.inv, [("4", "3")])  # c before b
        self.assertTrue(res.satisfiable)
        for cert in res.certificates:
            order = list(cert.glyph_order)
            self.assertLess(order.index("a"), order.index("b"))  # old table holds
        # 'c before b' is satisfiable only when c precedes b
        for cert in res.certificates:
            self.assertLess(cert.glyph_order.index("c"), cert.glyph_order.index("b"))

    def test_certificate_conflict_when_requirement_contradicts(self):
        res = certify_precedence(self.book, self.inv, [("3", "4"), ("4", "3")])
        self.assertFalse(res.satisfiable)
        self.assertIsNotNone(res.conflict)


# ----------------------------------------------------------------------
# revocation when a new loose page appears
# ----------------------------------------------------------------------
class RevocationTests(unittest.TestCase):
    def setUp(self):
        self.m = matrix("a", "b", "c")
        self.inv = CollationInvestigation(self.m).add_wordlist(
            wordlist("old", [("1", "a"), ("2", "b")], self.m)
        )
        self.book = book([("1", "aa"), ("2", "ab"), ("3", "b"), ("4", "c")], 1, self.m)
        self.solver = BookSolver(self.book, self.inv)

    def test_conclusion_forced_by_surviving_books_is_viable(self):
        clue = PositionClue("3", 3, 1)  # b in fascicle 3 -> c forced to fascicle 4
        concl = LocalConclusion(
            key="c@4", kind="position", entry_id="4", target=(4, 1),
            _clues=(clue,),
        )
        viable = viable_conclusions(self.book, self.inv, [concl], [clue])
        self.assertEqual([c.key for c in viable], ["c@4"])

    def test_new_clue_revokes_only_its_local_dependents(self):
        # Two *independent* local conclusions, each read off its own page:
        #   A) "entry 3 ('b') at fascicle 2"   <- page 1 said b@fasc2
        #   B) "entry 3 ('b') at fascicle 3"   <- page 2 said b@fasc3
        # They are mutually exclusive (same entry, different fascicle).
        page1 = PositionClue("3", 2, 1)
        page2 = PositionClue("3", 3, 1)
        concl_a = LocalConclusion(
            key="b@2", kind="position", entry_id="3", target=(2, 1),
            _clues=(page1,),
        )
        concl_b = LocalConclusion(
            key="b@3", kind="position", entry_id="3", target=(3, 1),
            _clues=(page2,),
        )
        # Newly found page says b is at fascicle 3 -> A is overturned, B survives.
        new_page = PositionClue("3", 3, 1)
        result = revoke_on_new_clue(
            self.book, self.inv, [concl_a, concl_b], new_page
        )
        self.assertEqual({c.key for c in result.retained}, {"b@3"})
        self.assertEqual({c.key for c in result.revoked}, {"b@2"})

    def test_new_clue_matching_both_keeps_both(self):
        page = PositionClue("3", 3, 1)
        concl = LocalConclusion(
            key="c@4", kind="position", entry_id="4", target=(4, 1),
            _clues=(page,),
        )
        # A new page agreeing with b@fasc3 keeps c@fasc4 viable.
        result = revoke_on_new_clue(
            self.book, self.inv, [concl], PositionClue("3", 3, 1)
        )
        self.assertEqual({c.key for c in result.retained}, {"c@4"})
        self.assertEqual(result.revoked, ())


# ----------------------------------------------------------------------
# integration: headwords read from real delivered packages
# ----------------------------------------------------------------------
def _package(chunks, snapshot_revs=None):
    snapshot = []
    for c in chunks:
        for sid, rev in zip(c["source_ids"], c["source_revisions"]):
            snapshot.append({"segment_id": sid, "revision": rev})
    return {
        "format": "translation-package/1",
        "package_id": "P",
        "document_id": "D",
        "language": "EN",
        "source_snapshot": snapshot,
        "chunks": chunks,
        "rendered_text": "\n".join(c["text"] for c in chunks),
    }


class PackageIntegrationTests(unittest.TestCase):
    def test_blocks_from_package_reads_revisions(self):
        pkg = _package(
            [
                {
                    "translation_id": "T1",
                    "source_ids": ["S1"],
                    "source_revisions": [1],
                    "text": "aa ab",
                }
            ]
        )
        blocks = blocks_from_package(pkg)
        self.assertEqual(blocks[0].source_revisions, {"S1": 1})

    def test_build_wordlist_uses_intervals_in_block_order(self):
        m = matrix("a", "b", "c")
        # one block "a b" -> headwords over exact char intervals
        pkg = _package(
            [
                {
                    "translation_id": "T1",
                    "source_ids": ["S1"],
                    "source_revisions": [1],
                    "text": "a b",
                }
            ]
        )
        blocks = blocks_from_package(pkg)
        marks = [
            HeadwordMark("2", "T1", 2, 3, "S1", 1),  # 'b'
            HeadwordMark("1", "T1", 0, 1, "S1", 1),  # 'a'
        ]
        wl = build_wordlist(blocks, marks, m, wordlist_id="old")
        # ordered by interval start within the block -> a then b
        self.assertEqual(wl.ids(), ("1", "2"))
        self.assertEqual([h.text for h in wl.headwords], ["a", "b"])

    def test_interval_out_of_bounds_rejected(self):
        m = matrix("a")
        pkg = _package(
            [{"translation_id": "T1", "source_ids": ["S1"], "source_revisions": [1], "text": "a"}]
        )
        blocks = blocks_from_package(pkg)
        with self.assertRaises(IntervalOutOfBounds):
            build_wordlist(
                blocks, [HeadwordMark("1", "T1", 0, 5, "S1", 1)], m, wordlist_id="o"
            )

    def test_untokenizable_interval_rejected(self):
        m = matrix("a", "b")
        pkg = _package(
            [{"translation_id": "T1", "source_ids": ["S1"], "source_revisions": [1], "text": "az"}]
        )
        blocks = blocks_from_package(pkg)
        with self.assertRaises(Untokenizable):
            build_wordlist(
                blocks, [HeadwordMark("1", "T1", 0, 2, "S1", 1)], m, wordlist_id="o"
            )

    def test_stale_paste_by_mark_revision_rejected(self):
        m = matrix("a")
        pkg = _package(
            [{"translation_id": "T1", "source_ids": ["S1"], "source_revisions": [2], "text": "a"}]
        )
        blocks = blocks_from_package(pkg)
        with self.assertRaises(StalePackage):
            build_wordlist(
                blocks, [HeadwordMark("1", "T1", 0, 1, "S1", 1)], m, wordlist_id="o"
            )

    def test_stale_paste_by_expected_edition_rejected(self):
        m = matrix("a")
        pkg = _package(
            [{"translation_id": "T1", "source_ids": ["S1"], "source_revisions": [1], "text": "a"}]
        )
        blocks = blocks_from_package(pkg)
        with self.assertRaises(StalePackage):
            build_wordlist(
                blocks,
                [HeadwordMark("1", "T1", 0, 1, "S1", 1)],
                m,
                wordlist_id="o",
                expected_revisions={"S1": 2},
            )

    def test_unknown_block_rejected(self):
        m = matrix("a")
        pkg = _package(
            [{"translation_id": "T1", "source_ids": ["S1"], "source_revisions": [1], "text": "a"}]
        )
        blocks = blocks_from_package(pkg)
        with self.assertRaises(UnknownBlock):
            build_wordlist(
                blocks, [HeadwordMark("1", "T9", 0, 1, "S1", 1)], m, wordlist_id="o"
            )

    def test_overlapping_marks_rejected(self):
        m = matrix("a", "b", "c")
        pkg = _package(
            [{"translation_id": "T1", "source_ids": ["S1"], "source_revisions": [1], "text": "abc"}]
        )
        blocks = blocks_from_package(pkg)
        with self.assertRaises(MalformedMark):
            build_wordlist(
                blocks,
                [
                    HeadwordMark("1", "T1", 0, 2, "S1", 1),
                    HeadwordMark("2", "T1", 1, 3, "S1", 1),
                ],
                m,
                wordlist_id="o",
            )

    def test_full_scenario_via_pipeline(self):
        pkg = _package(
            [
                {"translation_id": "OLD_A", "source_ids": ["S1"], "source_revisions": [1], "text": "a"},
                {"translation_id": "OLD_B", "source_ids": ["S1"], "source_revisions": [1], "text": "b"},
                {"translation_id": "B_AA", "source_ids": ["S2"], "source_revisions": [1], "text": "aa"},
                {"translation_id": "B_AB", "source_ids": ["S2"], "source_revisions": [1], "text": "ab"},
                {"translation_id": "B_B", "source_ids": ["S2"], "source_revisions": [1], "text": "b"},
                {"translation_id": "B_C", "source_ids": ["S2"], "source_revisions": [1], "text": "c"},
            ]
        )
        spec = {
            "matrix": ["a", "b", "c"],
            "wordlists": [
                {
                    "wordlist_id": "old",
                    "marks": [
                        {"entry_id": "1", "translation_id": "OLD_A", "start": 0, "end": 1, "segment_id": "S1", "source_revision": 1},
                        {"entry_id": "2", "translation_id": "OLD_B", "start": 0, "end": 1, "segment_id": "S1", "source_revision": 1},
                    ],
                }
            ],
            "book": {
                "book_id": "book",
                "fascicle_capacity": 1,
                "marks": [
                    {"entry_id": "1", "translation_id": "B_AA", "start": 0, "end": 2, "segment_id": "S2", "source_revision": 1},
                    {"entry_id": "2", "translation_id": "B_AB", "start": 0, "end": 2, "segment_id": "S2", "source_revision": 1},
                    {"entry_id": "3", "translation_id": "B_B", "start": 0, "end": 1, "segment_id": "S2", "source_revision": 1},
                    {"entry_id": "4", "translation_id": "B_C", "start": 0, "end": 1, "segment_id": "S2", "source_revision": 1},
                ],
                "queries": ["4"],
            },
            "clues": [{"kind": "position", "entry_id": "3", "fascicle": 3, "position": 1}],
            "handoff": "both",
            "requirements": [["4", "3"]],
        }
        report = run_scenario(spec, [pkg])
        self.assertTrue(report["investigation"]["feasible"])
        q = report["queries"]["4"]
        self.assertTrue(q["unique"])
        self.assertEqual(q["signed_position"], [4, 1])
        self.assertIn("slip", report)
        self.assertEqual(report["slip"]["distinct_book_orders"], 1)
        self.assertIn("certificates", report)
        self.assertTrue(report["certificates"]["satisfiable"])


class DemoAndCliTests(unittest.TestCase):
    def test_run_collation_demo_all_checks(self):
        import tempfile

        from delivery_system.collation.demo import run_collation_demo

        with tempfile.TemporaryDirectory() as tmp:
            report = run_collation_demo(tmp)
        self.assertTrue(report["ok"], [c for c in report["checks"] if not c["ok"]])
        self.assertTrue(all(c["ok"] for c in report["checks"]))

    def test_cli_demo_and_run(self):
        import os
        import subprocess
        import sys
        import tempfile

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-m", "delivery_system", "collation-demo"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])

        mat = os.path.join(root, "materials", "collation")
        proc = subprocess.run(
            [
                sys.executable, "-m", "delivery_system", "collation-run",
                "--spec", os.path.join(mat, "scenario_3_loose_page.json"),
                "--package", os.path.join(mat, "packages", "PKG_OLD_AB.json"),
                "--package", os.path.join(mat, "packages", "PKG_NEW4.json"),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["queries"]["4"]["signed_position"], [4, 1])

    def test_material_packages_are_valid(self):
        import os

        from delivery_system.package_io import validate_package

        mat = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "materials",
            "collation",
            "packages",
        )
        for name in os.listdir(mat):
            if name.endswith(".json"):
                with open(os.path.join(mat, name), encoding="utf-8") as fh:
                    content = json.load(fh)
                self.assertEqual(validate_package(content), [], name)

    def test_ligature_material_scenario(self):
        import os

        mat = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "materials",
            "collation",
        )
        with open(os.path.join(mat, "scenario_5_ligature.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        contents = []
        for name in ("PKG_OLD_ABC.json", "PKG_LIGCONF.json"):
            with open(os.path.join(mat, "packages", name), encoding="utf-8") as fh:
                contents.append(json.load(fh))
        report = run_scenario(spec, contents)
        self.assertTrue(report["investigation"]["feasible"])
        q = report["queries"]["E0"]
        self.assertFalse(q["satisfiable"])
        self.assertEqual(q["conflict"]["kind"], "loose_pages")


if __name__ == "__main__":
    unittest.main()

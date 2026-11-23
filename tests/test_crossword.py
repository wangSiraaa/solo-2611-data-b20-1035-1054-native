"""Tests for the bilingual crossword layer (stdlib unittest).

Encodes the worked scenario from the brief plus the offline, withdrawal,
double-credential, and handoff requirements.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from delivery_system.crossword import (
    BoardDefinition,
    ClueRegistry,
    CoinageRepository,
    Credential,
    CredentialPart,
    RuleWalker,
    Slot,
    TrialBoard,
    WordFormBook,
    analyze_board,
    build_answer_book,
)
from delivery_system.crossword.answerbook import resolve_clue
from delivery_system.crossword.errors import (
    BannedCombination,
    ClueNotFound,
    CoinageCoverageError,
    NoCredential,
    SheetBlockMismatch,
    SheetImmutable,
)
from delivery_system.crossword.handoff import CharRule, Ticket
from delivery_system.delivery_repository import DeliveryRepository
from delivery_system.source_catalog import PublishedSourceReader, SourceCatalog


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.source_db = os.path.join(self.tmp, "source.db")
        self.delivery_db = os.path.join(self.tmp, "delivery.db")
        self.clue_db = os.path.join(self.tmp, "clues.db")
        self.coinage_db = os.path.join(self.tmp, "coinage.db")

        self.catalog = SourceCatalog(self.source_db)
        self.reader = PublishedSourceReader(self.source_db)
        self.repo = DeliveryRepository(self.delivery_db, self.reader)
        self.registry = ClueRegistry(self.clue_db, self.reader)

        # Source with two segments; one block per segment in the package.
        self.catalog.create_document(
            "DOC1",
            [
                {"segment_id": "S1", "text": "across material"},
                {"segment_id": "S2", "text": "down material"},
            ],
        )
        self.repo.add_translation(
            "B1", "DOC1", "XY", "block one", [{"segment_id": "S1", "revision": 1}]
        )
        self.repo.add_translation(
            "B2", "DOC1", "XY", "block two", [{"segment_id": "S2", "revision": 1}]
        )
        self.package = self.repo.publish_language("DOC1", "XY", "PK1")
        self.coinage = CoinageRepository(self.coinage_db, self.repo, self.registry)

        # Horizontal clue H: root a, suffixes {b, c} -> ab / ac
        self.registry.add_clue("H", "DOC1", "S1", 1, "H1")
        self.registry.add_suffix_class("HSC", "H", ["b", "c"])
        self.registry.add_root("HA", "H", "a", ["HSC"])

        # Vertical clue V: roots b or c, suffix {d} -> bd / cd
        self.registry.add_clue("V", "DOC1", "S2", 1, "V1")
        self.registry.add_suffix_class("VSC", "V", ["d"])
        self.registry.add_root("VB", "V", "b", ["VSC"])
        self.registry.add_root("VC", "V", "c", ["VSC"])

    def tearDown(self):
        self._tmp.cleanup()

    def horizontal_sheet(self, sheet_id="SH1", entries=None):
        entries = entries or [
            {"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "b"},
            {"entry_id": "h2", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "c"},
        ]
        return self.coinage.add_sheet(
            sheet_id, "DOC1", "XY", "PK1", "B1", entries
        )

    def vertical_sheet(self, sheet_id="SV1", entries=None):
        entries = entries or [
            {"entry_id": "v1", "clue_id": "V", "op": "root", "root_id": "VB", "suffix": "d"},
            {"entry_id": "v2", "clue_id": "V", "op": "root", "root_id": "VC", "suffix": "d"},
        ]
        return self.coinage.add_sheet(
            sheet_id, "DOC1", "XY", "PK1", "B2", entries
        )

    def book(self, sheets, withdrawn=None):
        return build_answer_book(
            ["H", "V"], sheets, self.registry, withdrawn=withdrawn
        )

    def board(self):
        return BoardDefinition(
            "BD1",
            (
                Slot("SH", "H", "across", ((0, 0), (0, 1))),
                Slot("SV", "V", "down", ((0, 1), (1, 1))),
            ),
        )

    def spellings(self, book):
        return {cid: answers.spellings_only for cid, answers in book.items()}

    def withdrawals(self, *sheets):
        return {s.sheet_id: self.coinage.withdrawn_entries(s.sheet_id) for s in sheets}


class ClueRegistryTests(Base):
    def test_clue_pinned_to_revision_must_exist(self):
        with self.assertRaises(ClueNotFound):
            self.registry.add_clue("X", "DOC1", "S1", 99, "X1")

    def test_root_requires_registered_suffix_class_of_same_clue(self):
        with self.assertRaises(Exception):
            self.registry.add_root("HB", "H", "x", ["VSC"])  # V's class

    def test_duplicate_clue_rejected(self):
        with self.assertRaises(Exception):
            self.registry.add_clue("H", "DOC1", "S1", 1, "H2")


class AnswerBookTests(Base):
    def test_base_book_has_both_spellings(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        book = self.book([h, v])
        self.assertEqual(set(book["H"].spellings_only), {"ab", "ac"})
        self.assertEqual(set(book["V"].spellings_only), {"bd", "cd"})

    def test_each_spelling_has_resolvable_credential(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        cred = resolve_clue("H", "ac", [h, v], self.registry)
        self.assertEqual(cred.spelling, "ac")
        self.assertEqual(cred.prov("root_id"), "HA")
        self.assertEqual(cred.prov("block_id"), "B1")

    def test_banned_combination_rejected_at_sheet_cut(self):
        self.registry.add_ban("ban1", "H", "HA", "c")
        with self.assertRaises(BannedCombination):
            self.horizontal_sheet()

    def test_banned_combination_rejected_in_book_for_memory_sheet(self):
        # Even a sheet assembled without the repository must be refused when
        # the answer book sees a banned root+suffix.
        from delivery_system.crossword.models import CoinageEntry, CoinageSheet

        self.registry.add_ban("ban1", "H", "HA", "c")
        sheet = CoinageSheet(
            sheet_id="MEM", document_id="DOC1", language="XY",
            package_id="PK1", block_id="B1",
            entries=(
                CoinageEntry("m1", "MEM", "H", "root", root_id="HA", suffix="c"),
            ),
        )
        with self.assertRaises(BannedCombination):
            build_answer_book(["H"], [sheet], self.registry)

    def test_copy_op_reuses_previous_root(self):
        entries = [
            {"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "b"},
            {"entry_id": "h2", "clue_id": "H", "op": "copy", "suffix": "c"},
        ]
        h = self.horizontal_sheet(entries=entries)
        book = build_answer_book(["H"], [h], self.registry)
        self.assertEqual(set(book["H"].spellings_only), {"ab", "ac"})

    def test_copy_op_is_scoped_per_clue(self):
        # A copy entry for clue H must not borrow clue V's root, even if V's
        # entry came last in the sheet.
        v = self.vertical_sheet()  # entries v1 (VB->bd), v2 (VC->cd)
        bad = self.coinage.add_sheet(
            "SH_BAD", "DOC1", "XY", "PK1", "B1",
            [
                {"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "b"},
                {"entry_id": "h2", "clue_id": "H", "op": "copy", "suffix": "c"},
            ],
        )
        # Interleave V's sheet so a naive global "previous root" would mis-copy.
        book = build_answer_book(["H", "V"], [v, bad], self.registry)
        self.assertEqual(set(book["H"].spellings_only), {"ab", "ac"})
        # The copy entry still resolves H's own root HA ("a"), not V's.
        for src in book["H"].spellings:
            if src.spelling == "ac":
                self.assertIn("a", src.spelling)

    def test_copy_without_prior_root_of_same_clue_rejected(self):
        with self.assertRaises(Exception):
            self.coinage.add_sheet(
                "SH_NOROOT", "DOC1", "XY", "PK1", "B1",
                [{"entry_id": "h1", "clue_id": "H", "op": "copy", "suffix": "b"}],
            )

    def test_literal_chars_appended(self):
        entries = [
            {"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA",
             "literal": "x", "suffix": "b"},
        ]
        h = self.horizontal_sheet(entries=entries)
        book = build_answer_book(["H"], [h], self.registry)
        self.assertEqual(set(book["H"].spellings_only), {"axb"})

    def test_suffix_not_in_class_rejected(self):
        entries = [
            {"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "z"},
        ]
        with self.assertRaises(CoinageCoverageError):
            self.horizontal_sheet(entries=entries)


class SheetBindingTests(Base):
    def test_sheet_must_point_at_covered_clue(self):
        # A sheet on block B1 (S1) that tries to coin clue V (S2) is rejected.
        with self.assertRaises(CoinageCoverageError):
            self.coinage.add_sheet(
                "BAD", "DOC1", "XY", "PK1", "B1",
                [{"entry_id": "x", "clue_id": "V", "op": "root", "root_id": "VB", "suffix": "d"}],
            )

    def test_same_body_different_block_number_cannot_reuse_sheet(self):
        # "Another package the same day": identical body text, but the actual
        # blocks are numbered differently.  A sheet cut for PK1/B1 must not
        # apply to PK2/C1.
        other_db = os.path.join(self.tmp, "delivery2.db")
        other = DeliveryRepository(other_db, self.reader)
        other.add_translation(
            "C1", "DOC1", "XY", "block one", [{"segment_id": "S1", "revision": 1}]
        )
        other.add_translation(
            "C2", "DOC1", "XY", "block two", [{"segment_id": "S2", "revision": 1}]
        )
        pk2 = other.publish_language("DOC1", "XY", "PK2")
        self.assertEqual(pk2["rendered_text"], self.package["rendered_text"])
        self.assertEqual([c["translation_id"] for c in pk2["chunks"]], ["C1", "C2"])

        # The coinage repository follows the *same* package store for PK2.
        repo2 = CoinageRepository(self.coinage_db, other, self.registry)
        sheet = self.horizontal_sheet()  # bound to PK1 / B1
        with self.assertRaises(SheetBlockMismatch):
            # Reusing PK1's stored sheet against the new package's block is
            # refused: the sheet is pinned to (PK1, B1), not (PK2, C1).
            repo2.assert_block(sheet.sheet_id, "PK2", "C1")
        # A fresh sheet cut for PK2/C1 is a distinct block, so it is accepted.
        repo2.add_sheet(
            "SH3", "DOC1", "XY", "PK2", "C1",
            [{"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "b"}],
        )
        # Re-adding SH1's *entries* under a brand-new sheet id for the new
        # block is a genuinely new sheet, not a reuse of the old one.
        repo2.add_sheet(
            "SH4", "DOC1", "XY", "PK2", "C1",
            [{"entry_id": "h2", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "c"}],
        )
        self.assertEqual(repo2.get_sheet("SH4").block_key(), ("PK2", "C1"))
        self.assertEqual(sheet.block_key(), ("PK1", "B1"))

    def test_sheet_immutable(self):
        self.horizontal_sheet()
        with self.assertRaises(SheetImmutable):
            self.coinage.add_sheet(
                "SH1", "DOC1", "XY", "PK1", "B1",
                [{"entry_id": "h9", "clue_id": "H", "op": "literal", "literal": "zz"}],
            )


class BoardSolverTests(Base):
    def test_different_senses_same_visible_word_one_board(self):
        # Two clues with different sense numbers both coin the visible word
        # "ab"; the board remains a single board on that shared letter.
        from delivery_system.crossword.models import (
            ClueAnswers,
            Credential,
            SpellingSource,
        )

        board = BoardDefinition(
            "B",
            (
                Slot("S1", "H", "across", ((0, 0), (0, 1))),
                Slot("S2", "H2", "down", ((0, 0), (1, 0))),
                Slot("S3", "V", "across", ((1, 0), (1, 1))),
            ),
        )

        def answers(cid, spelling):
            return ClueAnswers(
                cid,
                (
                    SpellingSource(
                        cid,
                        spelling,
                        (Credential(f"{cid}-c", "form", cid, spelling, ()),),
                    ),
                ),
            )

        spellings = {
            "H": answers("H", "ab").spellings_only,
            "H2": answers("H2", "ab").spellings_only,
            "V": answers("V", "bd").spellings_only,
        }
        result = analyze_board(board, spellings)
        self.assertEqual(result.status, "unique")
        self.assertEqual(
            result.solutions[0].grid(),
            {(0, 0): "a", (0, 1): "b", (1, 0): "b", (1, 1): "d"},
        )

    def test_erase_lock_and_rearrange_yields_other_board(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        book = self.book([h, v])
        spellings = self.spellings(book)
        tb = TrialBoard(self.board())
        tb.set_lock((0, 1), "b")
        first = tb.analyze(spellings)
        self.assertEqual(first.status, "unique")
        # Erase the pencil lock and rearrange: the other board comes back.
        tb.erase_lock((0, 1))
        self.assertEqual(tb.analyze(spellings).status, "ambiguous")
        tb.set_lock((0, 1), "c")
        second = tb.analyze(spellings)
        self.assertEqual(second.status, "unique")
        self.assertEqual(dict(second.solutions[0].assignments)["SV"], "cd")
        self.assertNotEqual(first.solutions[0].grid(), second.solutions[0].grid())

    def test_two_legal_boards_ambiguous_with_forced_cells(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        book = self.book([h, v])
        result = analyze_board(self.board(), self.spellings(book))
        self.assertEqual(result.status, "ambiguous")
        # corner cells are forced, the crossing is not.
        self.assertEqual(result.forced_cells[(0, 0)], "a")
        self.assertEqual(result.forced_cells[(1, 1)], "d")
        self.assertNotIn((0, 1), result.forced_cells)
        a, b = result.two_distinct_boards()
        self.assertNotEqual(a.grid(), b.grid())
        self.assertEqual(result.differing_cell, (0, 1))

    def test_direct_conflicting_locks_on_same_cell(self):
        h = self.horizontal_sheet()
        book = self.book([h])
        spellings = {"H": book["H"].spellings_only, "V": ("bd", "cd")}
        # Two locks pin the same cell to different letters.
        result = analyze_board(
            self.board(), spellings, [((0, 1), "b"), ((0, 1), "c")]
        )
        self.assertEqual(result.status, "contradictory")
        self.assertEqual(result.conflict.kind, "locks")
        self.assertEqual(
            {(r, c, ch) for r, c, ch in result.conflict.locks},
            {(0, 1, "b"), (0, 1, "c")},
        )

    def test_two_locks_that_cannot_hold_together(self):
        # Locks on two different cells of the same across slot that no single
        # spelling can satisfy are reported as a lock conflict.
        h = self.horizontal_sheet()
        book = self.book([h])
        spellings = {"H": book["H"].spellings_only, "V": ("bd", "cd")}
        result = analyze_board(self.board(), spellings, {(0, 0): "a", (0, 1): "z"})
        self.assertEqual(result.status, "contradictory")
        self.assertEqual(result.conflict.kind, "locks")
        # The minimiser should keep only the essential lock(s).
        self.assertTrue(result.conflict.locks)

    def test_unique_when_crossing_forced_by_lock(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        book = self.book([h, v])
        tb = TrialBoard(self.board())
        tb.set_lock((0, 1), "b")
        result = tb.analyze(self.spellings(book))
        self.assertEqual(result.status, "unique")
        self.assertEqual(result.solution_count, 1)
        self.assertEqual(
            dict(result.solutions[0].assignments), {"SH": "ab", "SV": "bd"}
        )
        self.assertIsNotNone(result.answer_grid())

    def test_withdraw_across_ac_forces_crossing_to_b(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        self.coinage.withdraw_spelling("SH1", "H", "ac")
        book = self.book([h, v], withdrawn=self.withdrawals(h, v))
        self.assertEqual(self.spellings(book)["H"], ("ab",))
        result = analyze_board(self.board(), self.spellings(book))
        self.assertEqual(result.status, "unique")
        self.assertEqual(result.forced_cells[(0, 1)], "b")
        self.assertEqual(dict(result.solutions[0].assignments)["SV"], "bd")

    def test_lock_c_on_stale_ordering_contradiction(self):
        # After withdrawing ac, locking the crossing to c must be reported as
        # a contradiction naming the offending lock, not silently accepted.
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        self.coinage.withdraw_spelling("SH1", "H", "ac")
        book = self.book([h, v], withdrawn=self.withdrawals(h, v))
        result = analyze_board(self.board(), self.spellings(book), {(0, 1): "c"})
        self.assertEqual(result.status, "contradictory")
        self.assertEqual(result.conflict.kind, "locks")
        self.assertIn((0, 1, "c"), result.conflict.locks)

    def test_no_length_matching_spelling_is_slot_conflict(self):
        h = self.horizontal_sheet()
        book = self.book([h])
        result = analyze_board(self.board(), {"H": ("ab",), "V": ()})
        self.assertEqual(result.status, "contradictory")
        self.assertEqual(result.conflict.kind, "slots")

    def test_two_credentials_same_spelling_still_one_board(self):
        # Two coinage sheets produce the same visible spelling "ab"; the board
        # must stay a single board, and the spelling must carry two credentials.
        h1 = self.horizontal_sheet("SH1", [
            {"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "b"},
        ])
        h2 = self.horizontal_sheet("SH1B", [
            {"entry_id": "h1b", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "b"},
        ])
        v = self.vertical_sheet()
        book = self.book([h1, h2, v])
        src = book["H"].source_for("ab")
        self.assertEqual(len(src.credentials), 2)
        result = analyze_board(self.board(), self.spellings(book))
        # ab and bd/cd at the crossing: still ambiguous (c is still available
        # only via ac, which is absent) -> actually H has only ab, so unique.
        self.assertEqual(result.status, "unique")


class OfflineProofTests(Base):
    def test_trial_board_save_load_and_reanalyze(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        book = self.book([h, v])
        tb = TrialBoard(self.board())
        tb.set_lock((0, 1), "c")
        path = os.path.join(self.tmp, "board.json")
        tb.save(path)
        restored = TrialBoard.load(path)
        self.assertEqual(restored.locks, {(0, 1): "c"})
        self.assertEqual(restored.board.slot("SH").cells, ((0, 0), (0, 1)))
        # Erase the lock offline and re-analyze without any database access.
        restored.erase_lock((0, 1))
        result = restored.analyze(self.spellings(book))
        self.assertEqual(result.status, "ambiguous")

    def test_issued_proof_survives_withdrawal(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        book = self.book([h, v])
        board_result = analyze_board(self.board(), self.spellings(book))
        proof_content = {
            "board": "BD1",
            "solution": dict(board_result.solutions[0].assignments),
            "credentials": {
                sid: list(creds)
                for sid, creds in board_result.solutions[0].credentials
            },
        }
        self.coinage.issue_proof("PR1", "PK1", "B1", proof_content)
        # Withdraw the coinage the proof used; the issued proof is unchanged.
        self.coinage.withdraw_coinage("SH1", "h2", "testing")
        stored = self.coinage.get_proof("PR1")
        self.assertEqual(stored, proof_content)
        # A new answer book no longer offers ac.
        new_book = self.book([h, v], withdrawn=self.withdrawals(h, v))
        self.assertEqual(self.spellings(new_book)["H"], ("ab",))

    def test_withdrawal_can_be_restored_in_new_revision(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        self.coinage.withdraw_coinage("SH1", "h2")
        shrunk = self.book([h, v], withdrawn=self.withdrawals(h, v))
        self.assertEqual(self.spellings(shrunk)["H"], ("ab",))
        # A fresh sheet (new revision) re-supplies ac without touching SH1.
        h_rev = self.coinage.add_sheet(
            "SH1_REV", "DOC1", "XY", "PK1", "B1",
            [{"entry_id": "h2r", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "c"}],
        )
        restored = self.book([h, h_rev, v])
        self.assertIn("ac", self.spellings(restored)["H"])
        # The original sheet's stored content is still intact.
        self.assertEqual(len(h.entries), 2)


class HandoffWordFormTests(Base):
    def _book_with_creds(self):
        h = self.horizontal_sheet()
        v = self.vertical_sheet()
        book = self.book([h, v])
        tickets = [
            Ticket("T1", "H", "HA", "b", "ab"),
            Ticket("T2", "H", "HA", "c", "ac"),
            Ticket("T3", "V", "VB", "d", "bd"),
            Ticket("T4", "V", "VC", "d", "cd"),
        ]
        return book, tickets

    def test_lookup_by_literal(self):
        book, tickets = self._book_with_creds()
        # Build a word-form book where every form carries a numbered ticket so
        # withdrawal can target a specific provenance.
        wfb = WordFormBook()
        for ticket in tickets:
            wfb.add_ticket(ticket)
        for clue_id, answers in book.items():
            for src in answers.spellings:
                ticket_id = {
                    "ab": "T1", "ac": "T2", "bd": "T3", "cd": "T4"
                }[src.spelling]
                wfb.add_form(
                    clue_id,
                    src.spelling,
                    Credential(
                        credential_id=f"{ticket_id}:{src.spelling}",
                        kind="form",
                        clue_id=clue_id,
                        spelling=src.spelling,
                        parts=(CredentialPart(kind="root", value=""),),
                        provenance=(("ticket_id", ticket_id),),
                    ),
                )
        self.assertEqual(wfb.lookup("H", "ac").prov("ticket_id"), "T2")
        self.assertEqual(set(wfb.spellings_for("H")), {"ab", "ac"})

    def test_withdraw_ticket_drops_family(self):
        book, _ = self._book_with_creds()
        wfb = WordFormBook()
        for clue_id, answers in book.items():
            for src in answers.spellings:
                ticket_id = {"ab": "T1", "ac": "T2", "bd": "T3", "cd": "T4"}[src.spelling]
                wfb.add_form(
                    clue_id,
                    src.spelling,
                    Credential(
                        credential_id=f"{ticket_id}:{src.spelling}",
                        kind="form", clue_id=clue_id, spelling=src.spelling,
                        parts=(), provenance=(("ticket_id", ticket_id),),
                    ),
                )
        touched = wfb.withdraw_ticket("T2")
        self.assertIn("H", touched)
        self.assertEqual(set(wfb.spellings_for("H")), {"ab"})
        with self.assertRaises(NoCredential):
            wfb.lookup("H", "ac")

    def test_same_word_two_provenances_survives_one_withdrawal(self):
        wfb = WordFormBook()
        for tid in ("T2a", "T2b"):
            wfb.add_form(
                "H", "ac",
                Credential(
                    credential_id=tid, kind="form", clue_id="H", spelling="ac",
                    parts=(), provenance=(("ticket_id", tid),),
                ),
            )
        wfb.withdraw_ticket("T2a")
        self.assertEqual(wfb.spellings_for("H"), ("ac",))
        self.assertEqual(wfb.lookup("H", "ac").prov("ticket_id"), "T2b")


class HandoffDispatchTests(unittest.TestCase):
    def test_dispatch_word_form_and_rule(self):
        from delivery_system.crossword import build_handoff

        rules = [CharRule("r0", "H", 0, "literal", "a"), CharRule("r1", "H", 1, "emit", "b")]
        tickets = [Ticket("T1", "H", "HA", "b", "ab")]
        walker = build_handoff("rule", tickets=tickets, rules=rules)
        self.assertEqual(walker.walk("H", "T1"), "ab")

    def test_dispatch_rejects_unknown_mode(self):
        from delivery_system.crossword import HandoffModeError, build_handoff

        with self.assertRaises(HandoffModeError):
            build_handoff("telepathy")


class HandoffRuleTests(Base):
    def test_walker_emits_word_with_copy_rules(self):
        rules = [
            CharRule("r0", "H", 0, "literal", "a"),
            CharRule("r1", "H", 1, "emit", "b"),
            # A long shared prefix then a suffix that copies position 0.
            CharRule("r2", "H", 2, "copy", source_offset=0),
        ]
        tickets = [Ticket("T1", "H", "HA", "b", "aba")]
        walker = RuleWalker.from_tickets_and_rules(tickets, rules)
        self.assertEqual(walker.walk("H", "T1"), "aba")
        trace = walker.trace("H", "T1")
        self.assertEqual([t[0] for t in trace], [0, 1, 2])
        self.assertEqual(trace[2], (2, "copy", "a"))

    def test_walker_reconciles_crossing_letters(self):
        rules = [CharRule("r0", "H", 0, "literal", "a"), CharRule("r1", "H", 1, "emit", "b")]
        tickets = [Ticket("T1", "H", "HA", "b", "ab")]
        walker = RuleWalker.from_tickets_and_rules(tickets, rules)
        # A crossing that agrees is accepted ...
        self.assertEqual(walker.walk("H", "T1", crossings={1: "b"}), "ab")
        # ... a crossing that disagrees is refused.
        with self.assertRaises(Exception):
            walker.walk("H", "T1", crossings={1: "c"})

    def test_walker_rejects_ticket_word_mismatch(self):
        rules = [CharRule("r0", "H", 0, "literal", "a"), CharRule("r1", "H", 1, "emit", "c")]
        tickets = [Ticket("T1", "H", "HA", "b", "ab")]  # ticket says "ab", rules emit "ac"
        walker = RuleWalker.from_tickets_and_rules(tickets, rules)
        with self.assertRaises(Exception):
            walker.walk("H", "T1")

    def test_replace_single_rule(self):
        rules = [CharRule("r0", "H", 0, "literal", "a"), CharRule("r1", "H", 1, "emit", "b")]
        tickets = [Ticket("T1", "H", "HA", "b", "ab")]
        walker = RuleWalker.from_tickets_and_rules(tickets, rules)
        self.assertEqual(walker.walk("H", "T1"), "ab")
        # Replace one withdrawn rule; the ticket is re-issued for the new word.
        walker.replace_rule("r1", CharRule("r1b", "H", 1, "emit", "c"))
        walker.add_ticket(Ticket("T1", "H", "HA", "c", "ac"))
        self.assertEqual(walker.walk("H", "T1"), "ac")

    def test_replace_rule_without_reticket_rejects_stale_word(self):
        rules = [CharRule("r0", "H", 0, "literal", "a"), CharRule("r1", "H", 1, "emit", "b")]
        tickets = [Ticket("T1", "H", "HA", "b", "ab")]
        walker = RuleWalker.from_tickets_and_rules(tickets, rules)
        walker.replace_rule("r1", CharRule("r1b", "H", 1, "emit", "c"))
        # Rules now emit "ac" but the ticket still claims "ab": refuse.
        with self.assertRaises(Exception):
            walker.walk("H", "T1")

    def test_withdraw_ticket_removes_spelling(self):
        rules = [CharRule("r0", "H", 0, "literal", "a"), CharRule("r1", "H", 1, "emit", "b")]
        tickets = [Ticket("T1", "H", "HA", "b", "ab"), Ticket("T2", "H", "HA", "c", "ac")]
        walker = RuleWalker.from_tickets_and_rules(tickets, rules)
        walker.withdraw_ticket("T2")
        self.assertEqual(walker.spellings_for("H"), ("ab",))


if __name__ == "__main__":
    unittest.main()

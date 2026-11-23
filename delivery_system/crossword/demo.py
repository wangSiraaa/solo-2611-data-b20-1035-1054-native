"""End-to-end demonstration of the bilingual crossword layer.

Reproduces the brief's worked example with lowercase Latin letters, then walks
through every offline / withdrawal requirement:

1. base board (ab/bd or ac/cd) is ambiguous; corner cells are forced;
2. withdrawing the across-coinage ``ac`` forces the crossing to ``b``;
3. locking the crossing to ``c`` is contradictory; locking to ``b`` is unique;
4. two different senses (clues) spelled into the same visible word;
5. an issued (print-bound) proof keeps its original provenance after a
   withdrawal, while a new answer book reflects it;
6. both offline handoff styles (word-form book, rule walker).

The demo writes its SQLite stores into a throw-away directory and returns a
JSON-able report; it never mutates anything outside that directory.
"""

from __future__ import annotations

import os
from typing import Any

from ..delivery_repository import DeliveryRepository
from ..source_catalog import PublishedSourceReader, SourceCatalog
from .answerbook import build_answer_book, resolve_clue
from .board import BoardDefinition, Slot, TrialBoard, analyze_board
from .clue_registry import ClueRegistry
from .handoff import CharRule, RuleWalker, Ticket, WordFormBook
from .models import Credential
from .repository import CoinageRepository


def _solution_view(solution) -> dict[str, Any]:
    return {
        "assignments": dict(solution.assignments),
        "cells": [[r, c, ch] for r, c, ch in solution.cells],
        "credentials": {sid: list(creds) for sid, creds in solution.credentials},
    }


def run_crossword_demo(out_dir: str) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    source_db = os.path.join(out_dir, "source.db")
    delivery_db = os.path.join(out_dir, "delivery.db")
    clue_db = os.path.join(out_dir, "clues.db")
    coinage_db = os.path.join(out_dir, "coinage.db")

    report: dict[str, Any] = {"ok": True, "steps": [], "checks": []}

    def step(name: str, payload: dict[str, Any]) -> None:
        report["steps"].append({"name": name, **payload})

    def check(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            report["ok"] = False

    # --- source side: two segments, one per clue, pinned at revision 1 -----
    catalog = SourceCatalog(source_db)
    reader = PublishedSourceReader(source_db)
    catalog.create_document(
        "DOC1",
        [
            {"segment_id": "S1", "text": "across fill material"},
            {"segment_id": "S2", "text": "down fill material"},
        ],
    )

    # --- editor side: clues, senses, roots, suffix classes ----------------
    registry = ClueRegistry(clue_db, reader)
    registry.add_clue("H", "DOC1", "S1", 1, "H1", surface="across")
    registry.add_suffix_class("HSC", "H", ["b", "c"])
    registry.add_root("HA", "H", "a", ["HSC"])

    registry.add_clue("V", "DOC1", "S2", 1, "V1", surface="down")
    registry.add_suffix_class("VSC", "V", ["d"])
    registry.add_root("VB", "V", "b", ["VSC"])
    registry.add_root("VC", "V", "c", ["VSC"])

    # A second clue with a *different sense* that coins the same visible word
    # "ab" (sense H2) — two clues, one visible spelling.
    registry.add_clue("H2", "DOC1", "S1", 1, "H2", surface="across-alt")
    registry.add_suffix_class("H2SC", "H2", ["b"])
    registry.add_root("H2A", "H2", "a", ["H2SC"])

    # --- delivery side: blocks in a package -------------------------------
    repo = DeliveryRepository(delivery_db, reader)
    repo.add_translation(
        "B1", "DOC1", "XY", "across block", [{"segment_id": "S1", "revision": 1}]
    )
    repo.add_translation(
        "B2", "DOC1", "XY", "down block", [{"segment_id": "S2", "revision": 1}]
    )
    package = repo.publish_language("DOC1", "XY", "PK1")
    step("package", {"package_id": "PK1", "chunks": [c["translation_id"] for c in package["chunks"]]})

    # --- translator side: coinage sheets bound to exact blocks ------------
    coinage = CoinageRepository(coinage_db, repo, registry)
    sheet_h = coinage.add_sheet(
        "SH", "DOC1", "XY", "PK1", "B1",
        [
            {"entry_id": "h1", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "b"},
            {"entry_id": "h2", "clue_id": "H", "op": "root", "root_id": "HA", "suffix": "c"},
            {"entry_id": "h2b", "clue_id": "H2", "op": "root", "root_id": "H2A", "suffix": "b"},
        ],
    )
    sheet_v = coinage.add_sheet(
        "SV", "DOC1", "XY", "PK1", "B2",
        [
            {"entry_id": "v1", "clue_id": "V", "op": "root", "root_id": "VB", "suffix": "d"},
            {"entry_id": "v2", "clue_id": "V", "op": "root", "root_id": "VC", "suffix": "d"},
        ],
    )

    def book(withdrawn=None):
        return build_answer_book(
            ["H", "V", "H2"], [sheet_h, sheet_v], registry, withdrawn=withdrawn
        )

    def spellings(b):
        return {cid: answers.spellings_only for cid, answers in b.items()}

    base = book()
    step("answer_book_base", {"spellings": spellings(base)})
    check("H_has_ab_ac", set(base["H"].spellings_only) == {"ab", "ac"})
    check("V_has_bd_cd", set(base["V"].spellings_only) == {"bd", "cd"})
    check("H2_same_visible_word_ab", base["H2"].spellings_only == ("ab",))

    # Provenance lookup for one spelling.
    cred = resolve_clue("H", "ac", [sheet_h, sheet_v], registry)
    step(
        "provenance_ac",
        {
            "credential_id": cred.credential_id,
            "package_id": cred.prov("package_id"),
            "block_id": cred.prov("block_id"),
            "root_id": cred.prov("root_id"),
            "parts": [[p.kind, p.value, p.detail] for p in cred.parts],
        },
    )
    check("provenance_block_is_B1", cred.prov("block_id") == "B1")

    # --- board ------------------------------------------------------------
    board = BoardDefinition(
        "BD1",
        (
            Slot("SH", "H", "across", ((0, 0), (0, 1))),
            Slot("SV", "V", "down", ((0, 1), (1, 1))),
        ),
    )

    def analyse(locks=None, b=None):
        return analyze_board(board, spellings(b or base), locks)

    r0 = analyse()
    step(
        "base_board",
        {
            "status": r0.status,
            "forced_cells": {str(k): v for k, v in r0.forced_cells.items()},
            "differing_cell": r0.differing_cell,
        },
    )
    check("base_ambiguous", r0.status == "ambiguous")
    a_sol, b_sol = r0.two_distinct_boards()
    check("two_boards_differ", a_sol.grid() != b_sol.grid())
    check("corner_a_forced", r0.forced_cells.get((0, 0)) == "a")
    check("corner_d_forced", r0.forced_cells.get((1, 1)) == "d")
    check("crossing_not_forced", (0, 1) not in r0.forced_cells)

    # --- withdraw across coinage ac in the new translation; crossing -> b --
    coinage.withdraw_spelling("SH", "H", "ac")
    withdrawn = {
        sheet_h.sheet_id: coinage.withdrawn_entries(sheet_h.sheet_id),
        sheet_v.sheet_id: coinage.withdrawn_entries(sheet_v.sheet_id),
    }
    shrunk = book(withdrawn=withdrawn)
    step("answer_book_after_withdraw", {"spellings": spellings(shrunk)})
    check("H_only_ab_after_withdraw", shrunk["H"].spellings_only == ("ab",))
    r1 = analyse(b=shrunk)
    check("crossing_forced_b", r1.forced_cells.get((0, 1)) == "b")
    check("unique_after_withdraw", r1.status == "unique")
    check("vertical_bd", dict(r1.solutions[0].assignments).get("SV") == "bd")

    # --- lock the crossing to c on the (now stale) board: contradiction ---
    r2 = analyse(locks={(0, 1): "c"}, b=shrunk)
    step(
        "lock_c_contradiction",
        {"status": r2.status, "conflict": None if r2.conflict is None else {
            "kind": r2.conflict.kind, "locks": [list(x) for x in r2.conflict.locks],
            "detail": r2.conflict.detail}},
    )
    check("lock_c_contradictory", r2.status == "contradictory")
    check("lock_c_names_lock", (0, 1, "c") in (r2.conflict.locks if r2.conflict else ()))

    # --- issue a print proof, then withdraw after it is sent to print -----
    proof = {
        "board": "BD1",
        "solution": dict(r1.solutions[0].assignments),
        "credentials": {sid: list(c) for sid, c in r1.solutions[0].credentials},
    }
    coinage.issue_proof("PR1", "PK1", "B1", proof)
    # A later withdrawal must not mutate the sent-to-print proof.
    coinage.withdraw_coinage("SH", "h1", "post-print correction")
    post = book(withdrawn={
        "SH": coinage.withdrawn_entries("SH"),
        "SV": coinage.withdrawn_entries("SV"),
    })
    step("proof_after_later_withdraw", {"proof": coinage.get_proof("PR1")})
    check("proof_unchanged", coinage.get_proof("PR1") == proof)
    check("new_book_loses_ab_too", post["H"].spellings_only == ())

    # --- reopen the trial board offline (save -> load -> erase -> reanalyse) -
    trial = TrialBoard(board)
    trial.set_lock((0, 1), "b")
    board_path = os.path.join(out_dir, "trial_board.json")
    trial.save(board_path)
    restored = TrialBoard.load(board_path)
    restored.erase_lock((0, 1))
    r3 = restored.analyze(spellings(base))
    step("reopened_board", {"status": r3.status})
    check("reopened_ambiguous_again", r3.status == "ambiguous")

    # --- handoff style 1: word-form book ----------------------------------
    wfb = WordFormBook()
    ticket_map = {"ab": "T1", "ac": "T2", "bd": "T3", "cd": "T4"}
    for tid, (cid, sp) in zip(
        ticket_map.values(),
        [("H", "ab"), ("H", "ac"), ("V", "bd"), ("V", "cd")],
    ):
        wfb.add_ticket(Ticket(tid, cid, "root", sp[-1], sp))
        wfb.add_form(
            cid, sp,
            Credential(
                credential_id=f"{tid}:{sp}", kind="form", clue_id=cid,
                spelling=sp, parts=(), provenance=(("ticket_id", tid),),
            ),
        )
    step("word_form_book", {"H": list(wfb.spellings_for("H"))})
    touched = wfb.withdraw_ticket("T2")
    check("word_form_withdraw_touches_H", "H" in touched)
    check("word_form_H_only_ab", wfb.spellings_for("H") == ("ab",))

    # --- handoff style 2: rule walker -------------------------------------
    rules = [
        CharRule("r0", "H", 0, "literal", "a"),
        CharRule("r1", "H", 1, "emit", "b"),
    ]
    walker = RuleWalker.from_tickets_and_rules(
        [Ticket("R1", "H", "HA", "b", "ab")], rules
    )
    walked = walker.walk("H", "R1", crossings={1: "b"})
    trace = walker.trace("H", "R1")
    step("rule_walker", {"word": walked, "trace": [list(t) for t in trace]})
    check("walker_emits_ab", walked == "ab")
    # Replace one withdrawn rule and re-issue the numbered ticket for the new
    # word; the shop genuinely re-walks the shared prefix.
    walker.replace_rule("r1", CharRule("r1c", "H", 1, "emit", "c"))
    walker.add_ticket(Ticket("R1", "H", "HA", "c", "ac"))
    check("walker_after_replace_ac", walker.walk("H", "R1") == "ac")

    report["spellings_base"] = spellings(base)
    report["spellings_after_withdraw"] = spellings(shrunk)
    return report

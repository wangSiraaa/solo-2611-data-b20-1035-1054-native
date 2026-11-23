"""Command-line commands for the bilingual crossword layer.

These reuse the same database paths as the core CLI (``--source-db``,
``--delivery-db``) and add ``--clue-db`` / ``--coinage-db`` for the crossword
side.  Everything runs offline: ``xw-board`` can re-analyze a saved trial board
and re-print a proof without contacting any network service.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..delivery_repository import DeliveryRepository
from ..errors import DeliveryError
from ..source_catalog import PublishedSourceReader
from .answerbook import build_answer_book
from .board import TrialBoard
from .clue_registry import ClueRegistry
from .repository import CoinageRepository


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _paths(args: argparse.Namespace) -> tuple[str, str, str, str]:
    clue_db = getattr(args, "clue_db", None)
    coinage_db = getattr(args, "coinage_db", None)
    if not clue_db or not coinage_db:
        raise DeliveryError("crossword commands need --clue-db and --coinage-db")
    return args.source_db, args.delivery_db, clue_db, coinage_db


def _registry(args: argparse.Namespace) -> ClueRegistry:
    _s, _d, clue_db, _c = _paths(args)
    return ClueRegistry(clue_db, PublishedSourceReader(args.source_db))


def _coinage(args: argparse.Namespace) -> tuple[CoinageRepository, ClueRegistry]:
    _s, delivery_db, clue_db, coinage_db = _paths(args)
    reader = PublishedSourceReader(args.source_db)
    registry = ClueRegistry(clue_db, reader)
    repo = DeliveryRepository(delivery_db, reader)
    return CoinageRepository(coinage_db, repo, registry), registry


def cmd_xw_clue_add(args: argparse.Namespace) -> int:
    registry = _registry(args)
    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    added = {"clues": 0, "suffix_classes": 0, "roots": 0, "bans": 0}
    document_id = data["document_id"]
    for clue in data.get("clues", []):
        registry.add_clue(
            clue["clue_id"],
            document_id,
            clue["segment_id"],
            int(clue["revision"]),
            clue["sense_no"],
            clue.get("surface", ""),
        )
        added["clues"] += 1
    for sc in data.get("suffix_classes", []):
        registry.add_suffix_class(sc["class_id"], sc["clue_id"], sc["suffixes"])
        added["suffix_classes"] += 1
    for root in data.get("roots", []):
        registry.add_root(
            root["root_id"], root["clue_id"], root["letters"],
            root.get("suffix_class_ids", []),
        )
        added["roots"] += 1
    for ban in data.get("bans", []):
        registry.add_ban(ban["ban_id"], ban["clue_id"], ban["root_id"], ban["suffix"])
        added["bans"] += 1
    _emit({"document_id": document_id, "added": added})
    return 0


def cmd_xw_sheet_add(args: argparse.Namespace) -> int:
    coinage, _ = _coinage(args)
    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    sheet = coinage.add_sheet(
        data["sheet_id"],
        data["document_id"],
        data["language"],
        data["package_id"],
        data["block_id"],
        data.get("entries", []),
        data.get("bans", []),
    )
    _emit(
        {
            "sheet_id": sheet.sheet_id,
            "package_id": sheet.package_id,
            "block_id": sheet.block_id,
            "entries": len(sheet.entries),
        }
    )
    return 0


def _answer_book(args: argparse.Namespace):
    coinage, registry = _coinage(args)
    with open(args.clue_ids, encoding="utf-8") as fh:
        clue_ids = json.load(fh)
    sheets = [
        coinage.get_sheet(sid)
        for sid in (args.sheets or _all_sheet_ids(coinage))
    ]
    withdrawn = {s.sheet_id: coinage.withdrawn_entries(s.sheet_id) for s in sheets}
    return build_answer_book(clue_ids, sheets, registry, withdrawn=withdrawn)


def _all_sheet_ids(coinage: CoinageRepository) -> list[str]:
    return [s.sheet_id for s in coinage.list_sheets()]


def cmd_xw_book(args: argparse.Namespace) -> int:
    book = _answer_book(args)
    _emit(
        {
            cid: {
                "spellings": [
                    {
                        "spelling": src.spelling,
                        "credentials": [c.credential_id for c in src.credentials],
                    }
                    for src in answers.spellings
                ]
            }
            for cid, answers in book.items()
        }
    )
    return 0


def cmd_xw_board(args: argparse.Namespace) -> int:
    book = _answer_book(args)
    spellings = {cid: answers.spellings_only for cid, answers in book.items()}
    trial = TrialBoard.load(args.board)
    for lock in args.lock or []:
        row, col, letter = lock.split(":", 2)
        trial.set_lock((int(row), int(col)), letter)
    for cell in args.erase or []:
        row, col = cell.split(":", 1)
        trial.erase_lock((int(row), int(col)))
    result = trial.analyze(spellings)
    payload: dict[str, Any] = {
        "status": result.status,
        "forced_cells": [{"row": r, "col": c, "letter": ch} for (r, c), ch in sorted(result.forced_cells.items())],
        "cell_domain": {
            f"{r},{c}": sorted(dom) for (r, c), dom in sorted(result.cell_domain.items())
        },
    }
    if result.status == "unique":
        payload["answer_grid"] = [
            {"row": r, "col": c, "letter": ch}
            for r, c, ch in result.solutions[0].cells
        ]
    elif result.status == "ambiguous":
        pair = result.two_distinct_boards()
        payload["boards"] = [
            {"assignments": dict(sol.assignments)}
            for sol in pair
        ]
        payload["differing_cell"] = list(result.differing_cell) if result.differing_cell else None
    else:
        payload["conflict"] = None if result.conflict is None else {
            "kind": result.conflict.kind,
            "locks": [list(x) for x in result.conflict.locks],
            "slots": list(result.conflict.slots),
            "detail": result.conflict.detail,
        }
    _emit(payload)
    return 0 if result.status != "contradictory" else 3


def cmd_xw_demo(args: argparse.Namespace) -> int:
    import tempfile

    from .demo import run_crossword_demo

    with tempfile.TemporaryDirectory() as tmp:
        report = run_crossword_demo(tmp)
    _emit(report)
    return 0 if report["ok"] else 3


def add_crossword_commands(add) -> None:
    sp = add("xw-demo", cmd_xw_demo, "run the bilingual crossword end-to-end demo")
    sp.add_argument("--clue-db", default=argparse.SUPPRESS)
    sp.add_argument("--coinage-db", default=argparse.SUPPRESS)

    sp = add("xw-clue-add", cmd_xw_clue_add, "load clue material from JSON")
    sp.add_argument("--clue-db", default=argparse.SUPPRESS)
    sp.add_argument("--coinage-db", default=argparse.SUPPRESS)
    sp.add_argument("--file", required=True)

    sp = add("xw-sheet-add", cmd_xw_sheet_add, "load a coinage sheet bound to a block")
    sp.add_argument("--clue-db", default=argparse.SUPPRESS)
    sp.add_argument("--coinage-db", default=argparse.SUPPRESS)
    sp.add_argument("--file", required=True)

    sp = add("xw-book", cmd_xw_book, "print the answer book for clue ids")
    sp.add_argument("--clue-db", default=argparse.SUPPRESS)
    sp.add_argument("--coinage-db", default=argparse.SUPPRESS)
    sp.add_argument("--clue-ids", required=True, help="JSON list of clue ids")
    sp.add_argument("--sheets", nargs="*", default=None)

    sp = add("xw-board", cmd_xw_board, "analyze a saved trial board offline")
    sp.add_argument("--clue-db", default=argparse.SUPPRESS)
    sp.add_argument("--coinage-db", default=argparse.SUPPRESS)
    sp.add_argument("--clue-ids", required=True)
    sp.add_argument("--sheets", nargs="*", default=None)
    sp.add_argument("--board", required=True, help="trial board JSON path")
    sp.add_argument("--lock", action="append", help="row:col:letter")
    sp.add_argument("--erase", action="append", help="row:col")

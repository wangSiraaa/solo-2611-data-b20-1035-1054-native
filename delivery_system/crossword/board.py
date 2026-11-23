"""Reopenable trial-board solver.

Given a :class:`BoardDefinition` (slots + clues), the spellings each clue may
fill, and optional pencil locks, the solver answers the editor's questions:

* which cells are *forced* (the same letter in every complete legal board);
* is the whole board unique (then an answer grid is available);
* if not unique, two complete boards that visibly differ;
* if contradictory, a set of locks or slot constraints that cannot hold
  simultaneously.

A slot whose clue has two coinage credentials for the *same* spelling still has
one filling option: spellings are deduplicated per clue, so printing it remains
one board.
"""

from __future__ import annotations

from typing import Iterable, Optional, Union

from .models import (
    BoardAnalysis,
    BoardDefinition,
    BoardSolution,
    Conflict,
    Slot,
    SpellingSource,
)

Locks = dict[tuple[int, int], str]
LockInput = Union[Locks, Iterable[tuple[tuple[int, int], str]]]
Option = tuple[str, tuple[str, ...]]  # (spelling, credential ids)

# Hard ceiling on how many complete boards we materialise at once.  Forced-cell
# reasoning does not depend on this: it is answered by targeted satisfiability
# queries, so an "ambiguous" verdict stays sound even for huge boards.
_ENUM_LIMIT = 2
_CHECK_LIMIT = 1


def _normalise(
    spellings_by_clue: dict[str, Union[Iterable[str], Iterable[SpellingSource]]]
) -> dict[str, list[Option]]:
    out: dict[str, list[Option]] = {}
    for clue_id, values in spellings_by_clue.items():
        seen: dict[str, list[str]] = {}
        for value in values:
            if isinstance(value, SpellingSource):
                spelling = value.spelling
                creds = list(value.credential_ids)
            else:
                spelling = str(value)
                creds = []
            bucket = seen.setdefault(spelling, [])
            for c in creds:
                if c not in bucket:
                    bucket.append(c)
        out[clue_id] = [(s, tuple(creds)) for s, creds in sorted(seen.items())]
    return out


def _coerce_locks(locks: Optional[LockInput]) -> tuple[Locks, Optional[Conflict]]:
    """Normalise lock input; detect two locks forcing one cell to two letters."""

    if locks is None:
        return {}, None
    if isinstance(locks, dict):
        return dict(locks), None
    seen: dict[tuple[int, int], set[str]] = {}
    for cell, letter in locks:
        seen.setdefault(cell, set()).add(letter)
    bad = {cell: letters for cell, letters in seen.items() if len(letters) > 1}
    if bad:
        cell, letters = sorted(bad.items())[0]
        return {}, Conflict(
            kind="locks",
            locks=tuple((cell[0], cell[1], ch) for ch in sorted(letters)),
            detail=f"cell {cell} is locked to conflicting letters {sorted(letters)}",
        )
    return {cell: next(iter(letters)) for cell, letters in seen.items()}, None


class _Search:
    """Backtracking search over slot->spelling assignments."""

    def __init__(
        self,
        board: BoardDefinition,
        options: dict[str, list[Option]],
        locks: Locks,
    ) -> None:
        self.board = board
        self.locks = locks
        self.domains: dict[str, list[Option]] = {}
        self.slot_by_id: dict[str, Slot] = {}
        for slot in board.slots:
            self.slot_by_id[slot.slot_id] = slot
            base = options.get(slot.clue_id, [])
            self.domains[slot.slot_id] = [
                opt for opt in base if len(opt[0]) == slot.length
            ]
        # Cells that at least one slot covers; locks elsewhere are inert.
        self.covered = {cell for slot in board.slots for cell in slot.cells}

    def active_locks(self) -> Locks:
        return {cell: ch for cell, ch in self.locks.items() if cell in self.covered}

    def empty_domains(self) -> list[str]:
        return [sid for sid, dom in self.domains.items() if not dom]

    def _complete(self, locks: Optional[Locks]) -> Locks:
        base = self.active_locks() if locks is None else locks
        return {cell: ch for cell, ch in base.items() if cell in self.covered}

    def solve(
        self, limit: int = _ENUM_LIMIT, locks: Optional[Locks] = None
    ) -> list[dict[str, Option]]:
        # ``locks`` is the *complete* pencil-lock set; ``None`` means the
        # search's own active locks.  Callers that minimise a conflict must
        # pass a full set so a removed lock is really removed.
        locks = self._complete(locks)
        results: list[dict[str, Option]] = []
        assignment: dict[str, Option] = {}
        cell_char: dict[tuple[int, int], str] = {}

        def candidate_ok(slot: Slot, spelling: str) -> Optional[list[tuple[tuple[int, int], str]]]:
            fresh: list[tuple[tuple[int, int], str]] = []
            for offset, cell in enumerate(slot.cells):
                ch = spelling[offset]
                if cell in locks and locks[cell] != ch:
                    return None
                if cell in cell_char:
                    if cell_char[cell] != ch:
                        return None
                else:
                    fresh.append((cell, ch))
            return fresh

        def recurse(remaining: set[str]) -> None:
            if len(results) >= limit:
                return
            if not remaining:
                results.append(dict(assignment))
                return
            # MRV: pick the slot with the fewest currently consistent options.
            best_sid: Optional[str] = None
            best_choices: Optional[list[tuple[Option, list[tuple[tuple[int, int], str]]]]] = None
            for sid in remaining:
                slot = self.slot_by_id[sid]
                choices = []
                for opt in self.domains[sid]:
                    fresh = candidate_ok(slot, opt[0])
                    if fresh is not None:
                        choices.append((opt, fresh))
                if best_choices is None or len(choices) < len(best_choices):
                    best_sid, best_choices = sid, choices
                    if not choices:
                        break
            if not best_choices:
                return
            assert best_sid is not None
            slot = self.slot_by_id[best_sid]
            remaining = remaining - {best_sid}
            for opt, fresh in best_choices:
                assignment[best_sid] = opt
                for cell, ch in fresh:
                    cell_char[cell] = ch
                recurse(remaining)
                for cell, _ in fresh:
                    del cell_char[cell]
                del assignment[best_sid]
                if len(results) >= limit:
                    return

        if self.empty_domains():
            return []
        recurse(set(self.domains))
        return results

    def satisfiable(self, locks: Optional[Locks] = None) -> bool:
        return bool(self.solve(limit=_CHECK_LIMIT, locks=locks))


def _solution_from(
    board: BoardDefinition,
    assignment: dict[str, Option],
) -> BoardSolution:
    ordered = {slot.slot_id: assignment[slot.slot_id] for slot in board.slots}
    cells: dict[tuple[int, int], str] = {}
    for slot in board.slots:
        spelling = ordered[slot.slot_id][0]
        for offset, cell in enumerate(slot.cells):
            cells[cell] = spelling[offset]
    cell_list = tuple((r, c, cells[(r, c)]) for (r, c) in sorted(cells))
    assignments = tuple(
        (slot.slot_id, ordered[slot.slot_id][0]) for slot in board.slots
    )
    credentials = tuple(
        (slot.slot_id, ordered[slot.slot_id][1]) for slot in board.slots
    )
    return BoardSolution(
        assignments=assignments,
        cells=cell_list,
        credentials=credentials,
    )


def _find_minimal_unsat_locks(
    search: _Search, locks: Locks
) -> Optional[tuple[tuple[int, int, str], ...]]:
    active = dict(locks)
    if search.satisfiable(active):
        return None
    # Deletion-based minimisation: drop each lock while the set stays unsat.
    items = sorted(active.items())
    changed = True
    while changed:
        changed = False
        for cell, ch in items:
            if cell not in active:
                continue
            trial = dict(active)
            del trial[cell]
            if not search.satisfiable(trial):
                active = trial
                changed = True
    return tuple((cell[0], cell[1], ch) for cell, ch in sorted(active.items()))


def _find_minimal_unsat_slots(
    board: BoardDefinition, options: dict[str, list[Option]], locks: Locks
) -> Optional[tuple[str, ...]]:
    active = [slot.slot_id for slot in board.slots]
    subset = {slot.slot_id: slot for slot in board.slots}
    if not _subset_unsat(board, subset, options, locks):
        return None
    changed = True
    while changed:
        changed = False
        for sid in list(active):
            trial = [s for s in active if s != sid]
            trial_map = {s: subset[s] for s in trial}
            if _subset_unsat(board, trial_map, options, locks):
                active = trial
                changed = True
    return tuple(active)


def _subset_unsat(
    board: BoardDefinition,
    slots: dict[str, Slot],
    options: dict[str, list[Option]],
    locks: Locks,
) -> bool:
    sub = BoardDefinition(board_id=board.board_id, slots=tuple(slots.values()))
    return not _Search(sub, options, locks).satisfiable()


def analyze_board(
    board: BoardDefinition,
    spellings_by_clue: dict[str, Union[Iterable[str], Iterable[SpellingSource]]],
    locks: Optional[LockInput] = None,
) -> BoardAnalysis:
    locks, direct = _coerce_locks(locks)
    options = _normalise(spellings_by_clue)

    if direct is not None:
        return BoardAnalysis(status="contradictory", conflict=direct)

    search = _Search(board, options, locks)
    empty = search.empty_domains()
    if empty:
        sid = sorted(empty)[0]
        slot = board.slot(sid)
        return BoardAnalysis(
            status="contradictory",
            conflict=Conflict(
                kind="slots",
                slots=(sid,),
                detail=(
                    f"slot {sid!r} (clue {slot.clue_id!r}, length {slot.length}) has "
                    "no spelling of the required length"
                ),
            ),
        )

    if not search.satisfiable():
        minimal_locks = _find_minimal_unsat_locks(search, search.active_locks())
        if minimal_locks:
            return BoardAnalysis(
                status="contradictory",
                conflict=Conflict(
                    kind="locks",
                    locks=minimal_locks,
                    detail="these pencil locks cannot all hold on any complete board",
                ),
            )
        minimal_slots = _find_minimal_unsat_slots(board, options, locks)
        return BoardAnalysis(
            status="contradictory",
            conflict=Conflict(
                kind="slots",
                slots=minimal_slots or tuple(s.slot_id for s in board.slots),
                detail="these slots cannot all be filled consistently",
            ),
        )

    raw = search.solve(limit=_ENUM_LIMIT)
    first_solution = _solution_from(board, raw[0])

    # Per-cell domains, answered soundly by targeted satisfiability queries:
    # a letter belongs to a cell's domain iff some complete legal board puts
    # it there.  A cell is forced iff its domain is a singleton.
    letters_per_cell: dict[tuple[int, int], set[str]] = {}
    for slot in board.slots:
        for offset, cell in enumerate(slot.cells):
            bucket = letters_per_cell.setdefault(cell, set())
            for spelling, _ in options.get(slot.clue_id, []):
                if len(spelling) == slot.length:
                    bucket.add(spelling[offset])

    cell_domain: dict[tuple[int, int], frozenset[str]] = {}
    forced: dict[tuple[int, int], str] = {}
    witness: dict[tuple[tuple[int, int], str], dict[str, Option]] = {}
    active = search.active_locks()
    for cell, letters in sorted(letters_per_cell.items()):
        ok: set[str] = set()
        for letter in sorted(letters):
            existing = active.get(cell)
            if existing is not None and existing != letter:
                continue
            trial = dict(active)
            trial[cell] = letter
            found = search.solve(limit=1, locks=trial)
            if found:
                ok.add(letter)
                witness[(cell, letter)] = found[0]
        cell_domain[cell] = frozenset(ok)
        if len(ok) == 1:
            forced[cell] = next(iter(ok))

    # The visible grid is unique exactly when every cell is forced.  Two slots
    # backed by different credentials but the same spelling collapse earlier,
    # so they cannot make a board look ambiguous on their own.
    ambiguous_cells = [cell for cell, dom in cell_domain.items() if len(dom) >= 2]
    if ambiguous_cells:
        cell = ambiguous_cells[0]
        letters = sorted(cell_domain[cell])
        a = _solution_from(board, witness[(cell, letters[0])])
        b = _solution_from(board, witness[(cell, letters[1])])
        return BoardAnalysis(
            status="ambiguous",
            solutions=(a, b),
            cell_domain=cell_domain,
            forced_cells=forced,
            differing_cell=cell,
        )

    return BoardAnalysis(
        status="unique",
        solutions=(first_solution,),
        cell_domain=cell_domain,
        forced_cells=forced,
    )


class TrialBoard:
    """A reopenable board: layout + pencil locks, persistable as JSON.

    The editor can lock a pencil letter, erase a lock, and re-run
    :meth:`analyze` offline from a stored proof without touching the source or
    delivery databases.
    """

    def __init__(self, board: BoardDefinition, locks: Optional[Locks] = None) -> None:
        self.board = board
        self.locks: Locks = dict(locks or {})

    def set_lock(self, cell: tuple[int, int], letter: str) -> None:
        self.locks[cell] = letter

    def erase_lock(self, cell: tuple[int, int]) -> None:
        self.locks.pop(cell, None)

    def analyze(
        self,
        spellings_by_clue: dict[str, Union[Iterable[str], Iterable[SpellingSource]]],
    ) -> BoardAnalysis:
        return analyze_board(self.board, spellings_by_clue, self.locks)

    def to_dict(self) -> dict[str, object]:
        return {
            "board_id": self.board.board_id,
            "slots": [
                {
                    "slot_id": s.slot_id,
                    "clue_id": s.clue_id,
                    "direction": s.direction,
                    "cells": [[r, c] for r, c in s.cells],
                }
                for s in self.board.slots
            ],
            "locks": [
                {"row": r, "col": c, "letter": ch}
                for (r, c), ch in sorted(self.locks.items())
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TrialBoard":
        slots = tuple(
            Slot(
                slot_id=str(s["slot_id"]),
                clue_id=str(s["clue_id"]),
                direction=str(s["direction"]),
                cells=tuple((int(r), int(c)) for r, c in s["cells"]),  # type: ignore[index]
            )
            for s in data["slots"]  # type: ignore[index]
        )
        locks = {
            (int(item["row"]), int(item["col"])): str(item["letter"])
            for item in data.get("locks", [])  # type: ignore[union-attr]
        }
        return cls(BoardDefinition(board_id=str(data["board_id"]), slots=slots), locks)

    def save(self, path: str) -> None:
        import json

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    @classmethod
    def load(cls, path: str) -> "TrialBoard":
        import json

        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

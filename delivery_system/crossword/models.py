"""Immutable value objects for the bilingual crossword layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ----------------------------------------------------------------------
# Editor side: clues, senses, roots, suffix classes, audited bans
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ClueSense:
    """One clue (谜面) pinned to an exact source segment revision.

    ``clue_id`` is the identity.  ``sense_no`` is the sense number the editor
    assigns to the clue (词义号).  ``segment_id``/``revision`` pin the clue to
    the exact source revision so a coinage sheet can be checked against the
    package snapshot instead of relying on position or text.
    """

    clue_id: str
    document_id: str
    segment_id: str
    revision: int
    sense_no: str
    surface: str = ""

    def anchor(self) -> tuple[str, int]:
        return (self.segment_id, self.revision)


@dataclass(frozen=True)
class SuffixClass:
    """A named class of usable suffix strings (可用词尾类别) for one clue."""

    class_id: str
    clue_id: str
    suffixes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.suffixes:
            raise ValueError("a suffix class must list at least one suffix")


@dataclass(frozen=True)
class Root:
    """A root (词根) and the suffix classes that may follow it."""

    root_id: str
    clue_id: str
    letters: str
    suffix_class_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.letters:
            raise ValueError("a root must have at least one letter")


@dataclass(frozen=True)
class Ban:
    """An audited root+suffix combination that must not be coined (禁例)."""

    ban_id: str
    clue_id: str
    root_id: str
    suffix: str


# ----------------------------------------------------------------------
# Translator side: coinage sheets
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class CoinageEntry:
    """One coinage line on a translator's sheet.

    ``op`` is one of:

    * ``root``    – use ``root_id``'s letters;
    * ``copy``    – copy the root of the most recent earlier entry that
                    resolved a root (复制此前刚选过的同一词根);
    * ``literal`` – no root at all; only literal characters.

    ``literal`` characters are appended after the root and before the suffix
    (补字面字符).  ``suffix`` may be empty; when present it must come from one
    of the root's registered suffix classes and must not be banned.
    """

    entry_id: str
    sheet_id: str
    clue_id: str
    op: str
    root_id: Optional[str] = None
    literal: str = ""
    suffix: str = ""

    def __post_init__(self) -> None:
        if self.op not in ("root", "copy", "literal"):
            raise ValueError(f"unknown coinage op {self.op!r}")
        if self.op == "root" and not self.root_id:
            raise ValueError("op 'root' requires root_id")
        if self.op == "copy" and self.root_id:
            raise ValueError("op 'copy' must not carry root_id")
        if self.op == "literal" and self.root_id:
            raise ValueError("op 'literal' must not carry root_id")


@dataclass(frozen=True)
class CoinageSheet:
    """A translator's coinage sheet for one exact translated block.

    The ``(package_id, block_id)`` pair is the identity of the translated
    block, so two packages that print the same body text but number their
    actual block differently can never share a sheet.
    """

    sheet_id: str
    document_id: str
    language: str
    package_id: str
    block_id: str
    entries: tuple[CoinageEntry, ...]
    bans: tuple[Ban, ...] = ()

    def block_key(self) -> tuple[str, str]:
        return (self.package_id, self.block_id)


# ----------------------------------------------------------------------
# Provenance / assembled spellings
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class CredentialPart:
    """One step of a coinage trace (root letter, suffix, literal...)."""

    kind: str
    value: str
    detail: str = ""


@dataclass(frozen=True)
class Credential:
    """A single provenance for a spelling (造词凭据 / 批条)."""

    credential_id: str
    kind: str  # "coinage" | "form" | "rule"
    clue_id: str
    spelling: str
    parts: tuple[CredentialPart, ...]
    provenance: tuple[tuple[str, str], ...] = ()

    def prov(self, key: str, default: Optional[str] = None) -> Optional[str]:
        for k, v in self.provenance:
            if k == key:
                return v
        return default

    def prov_dict(self) -> dict[str, str]:
        return dict(self.provenance)


@dataclass(frozen=True)
class SpellingSource:
    """One spelling a clue can fill, with every credential that backs it.

    Multiple credentials for the same visible spelling collapse into a single
    :class:`SpellingSource`; the board solver therefore still prints one grid.
    """

    clue_id: str
    spelling: str
    credentials: tuple[Credential, ...]

    @property
    def credential_ids(self) -> tuple[str, ...]:
        return tuple(c.credential_id for c in self.credentials)


@dataclass(frozen=True)
class ClueAnswers:
    """Every spelling a single clue can fill, in the answer book."""

    clue_id: str
    spellings: tuple[SpellingSource, ...]

    @property
    def spellings_only(self) -> tuple[str, ...]:
        return tuple(s.spelling for s in self.spellings)

    def source_for(self, spelling: str) -> Optional[SpellingSource]:
        for s in self.spellings:
            if s.spelling == spelling:
                return s
        return None

    def credential_for(self, spelling: str) -> Optional[Credential]:
        src = self.source_for(spelling)
        return src.credentials[0] if src and src.credentials else None


# ----------------------------------------------------------------------
# Layout / board
# ----------------------------------------------------------------------
Cell = tuple[int, int]


@dataclass(frozen=True)
class Slot:
    """An ordered run of grid cells that a clue fills (横槽/纵槽)."""

    slot_id: str
    clue_id: str
    direction: str  # "across" | "down"
    cells: tuple[Cell, ...]

    def __post_init__(self) -> None:
        if self.direction not in ("across", "down"):
            raise ValueError(f"unknown direction {self.direction!r}")
        if not self.cells:
            raise ValueError("a slot must cover at least one cell")

    @property
    def length(self) -> int:
        return len(self.cells)


@dataclass(frozen=True)
class BoardDefinition:
    board_id: str
    slots: tuple[Slot, ...]

    def slot(self, slot_id: str) -> Slot:
        for s in self.slots:
            if s.slot_id == slot_id:
                return s
        raise KeyError(slot_id)

    def cell_index(self) -> dict[Cell, list[tuple[str, int]]]:
        index: dict[Cell, list[tuple[str, int]]] = {}
        for slot in self.slots:
            for offset, cell in enumerate(slot.cells):
                index.setdefault(cell, []).append((slot.slot_id, offset))
        return index


@dataclass(frozen=True)
class BoardSolution:
    """One complete, legal filling of the board."""

    assignments: tuple[tuple[str, str], ...]  # slot_id -> spelling (board order)
    cells: tuple[tuple[int, int, str], ...]  # (row, col, letter)
    credentials: tuple[tuple[str, tuple[str, ...]], ...] = ()  # slot_id -> cred ids

    def assignment(self) -> dict[str, str]:
        return dict(self.assignments)

    def grid(self) -> dict[Cell, str]:
        return {(r, c): ch for r, c, ch in self.cells}


@dataclass(frozen=True)
class Conflict:
    """A minimal-ish set of locks/slots that cannot hold simultaneously."""

    kind: str  # "locks" | "slots" | "mixed"
    locks: tuple[tuple[int, int, str], ...] = ()
    slots: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class BoardAnalysis:
    """Result of solving one trial board."""

    status: str  # "unique" | "ambiguous" | "contradictory"
    solutions: tuple[BoardSolution, ...] = ()
    cell_domain: dict[Cell, frozenset[str]] = field(default_factory=dict)
    forced_cells: dict[Cell, str] = field(default_factory=dict)
    differing_cell: Optional[Cell] = None
    conflict: Optional[Conflict] = None

    @property
    def unique(self) -> bool:
        return self.status == "unique"

    @property
    def solution_count(self) -> int:
        return len(self.solutions)

    def answer_grid(self) -> Optional[dict[Cell, str]]:
        """The answer grid, available only when the whole board is unique."""
        if self.status == "unique" and self.solutions:
            return self.solutions[0].grid()
        return None

    def two_distinct_boards(self) -> Optional[tuple[BoardSolution, BoardSolution]]:
        """Two complete boards that visibly differ, when ambiguous."""
        if self.status != "ambiguous" or len(self.solutions) < 2:
            return None
        return (self.solutions[0], self.solutions[1])

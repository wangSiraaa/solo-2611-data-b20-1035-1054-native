"""Answer book: join clue-material (editor side) with coinage sheets (translator
side) into per-clue spellings, each carrying provenance.

The answer book is *derived*, never stored as truth.  Given a clue's registered
roots/suffix classes/bans and the coinage entries of one or more sheets cut for
a real ``(package_id, block_id)``, it computes:

* ``resolve_sheet_entries`` – what spelling each coinage entry yields, in sheet
  order (later ``copy`` entries reuse the most recently selected root);
* ``build_answer_book`` – every spelling a clue can fill, each with one or more
  credentials, and a reverse lookup ``resolve_clue(clue, spelling)`` that
  returns a still-valid credential for a requested spelling.

Withdrawn coinages are excluded from *new* books; an already-issued proof keeps
the credentials it was printed with (see the repository's issued-proof store).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from .errors import BannedCombination, NoCredential
from .models import (
    ClueAnswers,
    ClueSense,
    CoinageEntry,
    CoinageSheet,
    Credential,
    CredentialPart,
    SpellingSource,
)


class MaterialReader(Protocol):
    def get_clue(self, clue_id: str) -> ClueSense: ...

    def get_root(self, root_id: str): ...

    def roots_for(self, clue_id: str): ...

    def get_suffix_class(self, class_id: str): ...

    def is_banned(self, clue_id: str, root_id: str, suffix: str) -> bool: ...


def _entry_spelling(
    entry: CoinageEntry,
    prev_letters: Optional[str],
    material: MaterialReader,
) -> tuple[str, tuple[CredentialPart, ...], Optional[str], Optional[str]]:
    """Return (spelling, parts, root_id_used_or_None, root_letters_used_or_None)."""

    parts: list[CredentialPart] = []
    if entry.op == "literal":
        if entry.literal:
            parts.append(CredentialPart(kind="literal", value=entry.literal))
        return entry.literal, tuple(parts), None, None

    if entry.op == "root":
        root_id = entry.root_id or ""
        letters = material.get_root(root_id).letters
    else:  # copy
        if prev_letters is None:
            raise NoCredential(
                f"entry {entry.entry_id!r} copies a root but none precedes it"
            )
        root_id = None
        letters = prev_letters
    parts.append(
        CredentialPart(
            kind="root",
            value=letters,
            detail="copy" if entry.op == "copy" else f"root {root_id}",
        )
    )
    if entry.literal:
        parts.append(CredentialPart(kind="literal", value=entry.literal))
    if entry.suffix:
        parts.append(CredentialPart(kind="suffix", value=entry.suffix))
    spelling = letters + entry.literal + entry.suffix
    return spelling, tuple(parts), root_id, letters


def _check_ban(
    sheet: CoinageSheet,
    entry: CoinageEntry,
    root_id: Optional[str],
    material: MaterialReader,
) -> None:
    if not entry.suffix or root_id is None:
        return
    banned = any(
        b.clue_id == entry.clue_id
        and b.root_id == root_id
        and b.suffix == entry.suffix
        for b in sheet.bans
    ) or material.is_banned(entry.clue_id, root_id, entry.suffix)
    if banned:
        raise BannedCombination(
            f"entry {entry.entry_id!r} uses banned {root_id}+{entry.suffix!r}"
        )


@dataclass(frozen=True)
class _Resolved:
    entry: CoinageEntry
    spelling: str
    parts: tuple[CredentialPart, ...]
    root_id: Optional[str]
    letters: Optional[str]


def _iter_resolved(sheet: CoinageSheet, material: MaterialReader):
    """Resolve every entry in sheet order, tracking the last root *per clue*.

    "copy the previously selected root" is scoped to the same clue (同一词根):
    a copy entry never borrows another clue's root.  Ban checking is applied
    here so every consumer (book, resolve, withdrawal) sees the same rules.
    """

    prev_by_clue: dict[str, str] = {}
    for entry in sheet.entries:
        prev_letters = prev_by_clue.get(entry.clue_id)
        spelling, parts, root_id, letters = _entry_spelling(
            entry, prev_letters, material
        )
        if entry.suffix:
            check_root = root_id
            if check_root is None:
                check_root = _root_id_for_letters(entry, prev_letters, material)
            _check_ban(sheet, entry, check_root, material)
        if entry.op in ("root", "copy") and letters is not None:
            prev_by_clue[entry.clue_id] = letters
        yield _Resolved(entry, spelling, parts, root_id, letters)


def resolve_sheet_entries(
    sheet: CoinageSheet, material: MaterialReader
) -> list[tuple[str, str, str]]:
    """Yield ``(entry_id, clue_id, spelling)`` in sheet order.

    Raises :class:`BannedCombination` if an entry uses a banned root+suffix.
    """

    return [
        (r.entry.entry_id, r.entry.clue_id, r.spelling)
        for r in _iter_resolved(sheet, material)
    ]


def _root_id_for_letters(
    entry: CoinageEntry,
    prev_letters: Optional[str],
    material: MaterialReader,
) -> Optional[str]:
    """Best-effort root id for a ``copy`` entry, by matching letters among the
    clue's registered roots; ``root`` entries already carry their id."""

    if entry.op == "root":
        return entry.root_id
    if prev_letters is None:
        return None
    matches = [r.root_id for r in material.roots_for(entry.clue_id) if r.letters == prev_letters]
    return matches[-1] if matches else None


def _credential_for_entry(
    sheet: CoinageSheet,
    entry: CoinageEntry,
    spelling: str,
    parts: tuple[CredentialPart, ...],
    root_id: Optional[str],
) -> Credential:
    prov = [
        ("sheet_id", sheet.sheet_id),
        ("entry_id", entry.entry_id),
        ("package_id", sheet.package_id),
        ("block_id", sheet.block_id),
        ("op", entry.op),
    ]
    if root_id:
        prov.append(("root_id", root_id))
    return Credential(
        credential_id=f"{sheet.sheet_id}:{entry.entry_id}",
        kind="coinage",
        clue_id=entry.clue_id,
        spelling=spelling,
        parts=parts,
        provenance=tuple(prov),
    )


def _sheet_credentials(
    sheet: CoinageSheet, material: MaterialReader
) -> dict[str, list[Credential]]:
    """All coins a sheet produces, grouped by clue id, in sheet order."""

    by_clue: dict[str, list[Credential]] = {}
    for r in _iter_resolved(sheet, material):
        cred = _credential_for_entry(sheet, r.entry, r.spelling, r.parts, r.root_id)
        by_clue.setdefault(r.entry.clue_id, []).append(cred)
    return by_clue


def build_answer_book(
    clue_ids: Iterable[str],
    sheets: Iterable[CoinageSheet],
    material: MaterialReader,
    *,
    withdrawn: Optional[dict[str, set[str]]] = None,
) -> dict[str, ClueAnswers]:
    """Assemble per-clue spellings with credentials from the given sheets.

    ``withdrawn`` maps ``sheet_id -> {entry_id, ...}`` for coinages the
    lexicographer has withdrawn; those credentials are omitted from the new
    book (but a spelling survives if another credential still backs it).
    """

    withdrawn = withdrawn or {}
    wanted = list(dict.fromkeys(clue_ids))
    # clue -> spelling -> list of credentials
    buckets: dict[str, dict[str, list[Credential]]] = {cid: {} for cid in wanted}
    for sheet in sheets:
        skip = withdrawn.get(sheet.sheet_id, set())
        for clue_id, creds in _sheet_credentials(sheet, material).items():
            if clue_id not in buckets:
                continue
            for cred in creds:
                entry_id = cred.prov("entry_id")
                if entry_id in skip:
                    continue
                buckets[clue_id].setdefault(cred.spelling, []).append(cred)

    book: dict[str, ClueAnswers] = {}
    for clue_id, spellings in buckets.items():
        sources = tuple(
            SpellingSource(
                clue_id=clue_id,
                spelling=spelling,
                credentials=tuple(creds),
            )
            for spelling, creds in sorted(spellings.items())
        )
        book[clue_id] = ClueAnswers(clue_id=clue_id, spellings=sources)
    return book


def resolve_clue(
    clue_id: str,
    spelling: str,
    sheets: Iterable[CoinageSheet],
    material: MaterialReader,
    *,
    withdrawn: Optional[dict[str, set[str]]] = None,
) -> Credential:
    """Return a still-valid credential that produces ``spelling`` for a clue."""

    withdrawn = withdrawn or {}
    for sheet in sheets:
        skip = withdrawn.get(sheet.sheet_id, set())
        for r in _iter_resolved(sheet, material):
            if r.entry.clue_id != clue_id:
                continue
            if r.spelling == spelling and r.entry.entry_id not in skip:
                return _credential_for_entry(
                    sheet, r.entry, r.spelling, r.parts, r.root_id
                )
    raise NoCredential(
        f"no valid credential produces {spelling!r} for clue {clue_id!r}"
    )

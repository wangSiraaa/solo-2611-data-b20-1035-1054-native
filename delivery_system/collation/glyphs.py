"""Local type matrix, longest-match tokenisation and headword comparison.

A *type matrix* is the set of physical sorts (字模) available to a printer: it
contains single characters *and* whole-block ligatures.  A text is split into
sorts by scanning left to right and always taking the **longest** sort that fits
at the current offset.  If the text cannot be consumed completely the interval
is rejected (:class:`Untokenizable`).

Ordering two headwords is exactly the rule the brief gives:

1. compare the two sort sequences glyph by glyph -- the **first differing
   glyph** decides;
2. if one sequence is a proper prefix of the other, the **shorter word comes
   first**;
3. if the sequences are identical, break the tie by the **stable entry number**.

None of this needs a global permutation: a total order over glyphs is enough to
compare any pair, and the comparison is transitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from typing import Iterable, Sequence

from ..errors import MalformedMark, Untokenizable


def entry_id_key(entry_id: str) -> tuple[int, int, str]:
    """Deterministic, number-aware ordering key for a stable entry number.

    All-digit ids sort numerically (so ``"9" < "10"``); anything else sorts
    lexicographically after the numeric ones.  This is the tie-break used when
    two headwords tokenise identically.
    """

    text = str(entry_id)
    if text.isdigit():
        return (0, int(text), text)
    return (1, 0, text)


@dataclass(frozen=True)
class TypeMatrix:
    """The local set of sorts, supporting left-to-right longest-match splitting."""

    glyphs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.glyphs:
            raise MalformedMark("type matrix must contain at least one sort")
        for glyph in self.glyphs:
            if not isinstance(glyph, str) or glyph == "":
                raise MalformedMark("every sort must be a non-empty string")
        if len(set(self.glyphs)) != len(self.glyphs):
            raise MalformedMark("type matrix contains duplicate sorts")
        # Longest first, then deterministic lexical tie-break.
        ordered = tuple(sorted(self.glyphs, key=lambda g: (-len(g), g)))
        object.__setattr__(self, "_ordered", ordered)
        object.__setattr__(self, "_as_set", frozenset(self.glyphs))

    @property
    def ordered(self) -> tuple[str, ...]:
        return self._ordered

    def tokenize(self, word: str) -> tuple[str, ...]:
        """Split ``word`` fully by longest match.

        Raises :class:`Untokenizable` if any character cannot be covered.
        """

        if not isinstance(word, str):
            raise Untokenizable(f"headword {word!r} is not a string")
        if word == "":
            raise Untokenizable("headword is empty; an empty interval cannot be split")
        tokens: list[str] = []
        offset = 0
        n = len(word)
        ordered = self._ordered
        while offset < n:
            for glyph in ordered:
                if word.startswith(glyph, offset):
                    tokens.append(glyph)
                    offset += len(glyph)
                    break
            else:
                raise Untokenizable(
                    f"cannot split headword {word!r} at offset {offset} "
                    f"(remaining {word[offset:]!r}) with the local type matrix"
                )
        return tuple(tokens)

    def tokenize_all(self, words: Iterable[str]) -> tuple[tuple[str, ...], ...]:
        return tuple(self.tokenize(word) for word in words)

    def contains(self, glyph: str) -> bool:
        return glyph in self._as_set


@dataclass(frozen=True)
class GlyphOrder:
    """A strict total order (全序) over the *complete* set of sorts."""

    order: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(set(self.order)) != len(self.order):
            raise MalformedMark("a glyph order may not repeat a sort")
        object.__setattr__(self, "_rank", {g: i for i, g in enumerate(self.order)})

    @property
    def rank(self) -> dict[str, int]:
        return self._rank

    def covers(self, glyphs: Iterable[str]) -> bool:
        return all(g in self._rank for g in glyphs)

    def as_dict(self) -> dict[str, int]:
        return dict(self._rank)


def compare_tokens(
    left: Sequence[str],
    right: Sequence[str],
    rank: dict[str, int],
) -> int:
    """Compare two token sequences by the given glyph ranks.

    Returns ``-1`` (left first), ``0`` (identical sequence) or ``1``.
    Prefix-shorter-first is applied when one sequence is a proper prefix of the
    other.
    """

    for a, b in zip(left, right):
        if a != b:
            ra = rank.get(a)
            rb = rank.get(b)
            if ra is None or rb is None:
                missing = a if ra is None else b
                raise Untokenizable(
                    f"glyph {missing!r} is missing from the glyph order"
                )
            return -1 if ra < rb else 1
    if len(left) < len(right):
        return -1
    if len(left) > len(right):
        return 1
    return 0


def compare_headwords(
    left_tokens: Sequence[str],
    left_id: str,
    right_tokens: Sequence[str],
    right_id: str,
    rank: dict[str, int],
) -> int:
    """Full headword comparison: sorts first, then the stable entry number."""

    primary = compare_tokens(left_tokens, right_tokens, rank)
    if primary != 0:
        return primary
    ka = entry_id_key(left_id)
    kb = entry_id_key(right_id)
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


def headword_sort_key(rank: dict[str, int]):
    """A ``cmp_to_key`` factory for sorting ``(entry_id, tokens)`` records."""

    def compare(a, b) -> int:  # a,b are (id, tokens)
        return compare_headwords(a[1], a[0], b[1], b[0], rank)

    return cmp_to_key(compare)

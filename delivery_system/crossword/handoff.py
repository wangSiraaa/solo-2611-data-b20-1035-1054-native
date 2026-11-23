"""Two offline handoff styles between editor and print shop.

The brief lets the editor and print shop pick one of two agreed handoffs, each
with its own entry point.  Both must work fully offline on the proof that was
delivered, and both must be able to re-detect incompatibility at crossings.

1. :class:`WordFormBook` (逐词核准的词形册 + 各自稿源)
   The editor hands a per-clue list of approved word forms with their
   provenance.  The print shop joins forms by shared cells.  Looking a form up
   by its literal spelling is cheap; but withdrawing one suffix ticket can
   invalidate a whole family of forms, and when the same visible word has two
   provenances the shop must decide whether *another* credential still keeps
   the word alive.

2. :class:`RuleWalker` (逐字可走的造词规则 + 带编号的根尾批条)
   The editor hands numbered root/suffix tickets plus per-character generation
   rules.  The shop walks the shared cells to get letters, then emits a trace
   that produced the whole word.  Replacing a single withdrawn rule is allowed;
   the cost is that the shop must genuinely handle rewriting, bans, and the
   long prefix that only diverges at the suffix.

Both styles expose ``spellings_for(clue_id)`` returning the visible spellings
(so the same board solver can reason about crossings), and can apply
withdrawals against a proof offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .errors import HandoffModeError, NoCredential, RuleError
from .models import (
    ClueAnswers,
    Credential,
    CredentialPart,
)


# ======================================================================
# Style 1: word-form book
# ======================================================================
@dataclass(frozen=True)
class Ticket:
    """A numbered root/suffix ticket (根尾批条)."""

    ticket_id: str
    clue_id: str
    root_id: str
    suffix: str
    spelling: str

    @property
    def parts(self) -> tuple[CredentialPart, ...]:
        out = (
            CredentialPart(
                kind="root", value="", detail=f"ticket {self.ticket_id} root {self.root_id}"
            ),
        )
        if self.suffix:
            out = out + (CredentialPart(kind="suffix", value=self.suffix),)
        return out


class WordFormBook:
    """Per-word approved word forms, each with one or more source tickets.

    ``forms[clue_id][spelling] = [credential, ...]``.  Two credentials for one
    spelling keep the word alive if either survives a withdrawal.
    """

    def __init__(self) -> None:
        self.forms: dict[str, dict[str, list[Credential]]] = {}
        self.tickets: dict[str, Ticket] = {}
        self._withdrawn_tickets: set[str] = set()

    # ------------------------------------------------------------------
    # construction (editor entry point)
    # ------------------------------------------------------------------
    @classmethod
    def from_answer_book(
        cls, book: dict[str, ClueAnswers], tickets: Iterable[Ticket] = ()
    ) -> "WordFormBook":
        self = cls()
        for tid in tickets:
            self.add_ticket(tid)
        for clue_id, answers in book.items():
            for src in answers.spellings:
                for cred in src.credentials:
                    self.add_form(clue_id, src.spelling, cred)
        return self

    def add_ticket(self, ticket: Ticket) -> None:
        self.tickets[ticket.ticket_id] = ticket

    def add_form(self, clue_id: str, spelling: str, credential: Credential) -> None:
        self.forms.setdefault(clue_id, {}).setdefault(spelling, []).append(credential)

    # ------------------------------------------------------------------
    # shop entry point
    # ------------------------------------------------------------------
    def spellings_for(self, clue_id: str) -> tuple[str, ...]:
        """Spellings still backed by at least one non-withdrawn credential."""

        out: list[str] = []
        for spelling, creds in sorted(self.forms.get(clue_id, {}).items()):
            if self._survives(creds):
                out.append(spelling)
        return tuple(out)

    def versions_for(self, clue_id: str, spelling: str) -> int:
        return sum(1 for c in self.forms.get(clue_id, {}).get(spelling, []) if self._survives([c]))

    def lookup(self, clue_id: str, spelling: str) -> Credential:
        """Literal lookup by spelling; returns a surviving credential."""

        for cred in self.forms.get(clue_id, {}).get(spelling, []):
            if self._survives([cred]):
                return cred
        raise NoCredential(
            f"no surviving form credential for {clue_id!r}={spelling!r}"
        )

    def _survives(self, creds: Iterable[Credential]) -> bool:
        for cred in creds:
            ticket_id = cred.prov("ticket_id")
            if ticket_id is None or ticket_id not in self._withdrawn_tickets:
                return True
        return False

    # ------------------------------------------------------------------
    # offline withdrawal (may invalidate a whole family of forms)
    # ------------------------------------------------------------------
    def withdraw_ticket(self, ticket_id: str) -> list[str]:
        """Withdraw a ticket; return clue ids whose form set shrank.

        A spelling with two provenances stays alive if the other credential
        does not depend on the withdrawn ticket.
        """

        self._withdrawn_tickets.add(ticket_id)
        # Separate the forms that relied on this ticket from those that did.
        touched: list[str] = []
        for clue_id, by_spelling in self.forms.items():
            before = set(self._surviving_spellings(by_spelling, set()))
            after = set(self._surviving_spellings(by_spelling, self._withdrawn_tickets))
            if before != after:
                touched.append(clue_id)
        return touched

    @staticmethod
    def _surviving_spellings(
        by_spelling: dict[str, list[Credential]], withdrawn: set[str]
    ) -> list[str]:
        out: list[str] = []
        for spelling, creds in by_spelling.items():
            for cred in creds:
                ticket_id = cred.prov("ticket_id")
                if ticket_id is None or ticket_id not in withdrawn:
                    out.append(spelling)
                    break
        return out

    def withdrawn(self) -> set[str]:
        return set(self._withdrawn_tickets)


# ======================================================================
# Style 2: character rules + numbered tickets
# ======================================================================
@dataclass(frozen=True)
class CharRule:
    """A per-character generation rule keyed by position from the start.

    ``kind`` is ``"emit"`` (emit ``value``), ``"copy"`` (copy the character at
    ``source_offset``), or ``"literal"`` (emit ``value`` verbatim).  Rules may
    share a long prefix and only diverge at a suffix, which the walker must
    handle without special-casing.
    """

    rule_id: str
    clue_id: str
    offset: int
    kind: str
    value: str = ""
    source_offset: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind not in ("emit", "copy", "literal"):
            raise RuleError(f"unknown rule kind {self.kind!r}")


class RuleWalker:
    """Walk character rules against numbered tickets to emit whole words."""

    def __init__(self) -> None:
        self.rules: dict[str, list[CharRule]] = {}
        self.tickets: dict[str, Ticket] = {}
        self._withdrawn_rules: set[str] = set()
        self._withdrawn_tickets: set[str] = set()

    # ------------------------------------------------------------------
    # editor entry point
    # ------------------------------------------------------------------
    @classmethod
    def from_tickets_and_rules(
        cls, tickets: Iterable[Ticket], rules: Iterable[CharRule]
    ) -> "RuleWalker":
        self = cls()
        for tid in tickets:
            self.add_ticket(tid)
        for rule in rules:
            self.add_rule(rule)
        return self

    def add_ticket(self, ticket: Ticket) -> None:
        self.tickets[ticket.ticket_id] = ticket

    def add_rule(self, rule: CharRule) -> None:
        self.rules.setdefault(rule.clue_id, []).append(rule)

    # ------------------------------------------------------------------
    # shop entry point
    # ------------------------------------------------------------------
    def spellings_for(self, clue_id: str) -> tuple[str, ...]:
        return tuple(sorted({t.spelling for t in self._live_tickets(clue_id)}))

    def _live_tickets(self, clue_id: str) -> list[Ticket]:
        return [
            t
            for tid, t in self.tickets.items()
            if t.clue_id == clue_id
            and tid not in self._withdrawn_tickets
        ]

    def _emit(self, clue_id: str) -> tuple[list[str], list[tuple[int, str, str]]]:
        rules = sorted(
            (r for r in self.rules.get(clue_id, []) if r.rule_id not in self._withdrawn_rules),
            key=lambda r: r.offset,
        )
        if not rules:
            raise RuleError(f"no live rules for clue {clue_id!r}")
        emitted: dict[int, str] = {}
        steps: list[tuple[int, str, str]] = []
        for rule in rules:
            if rule.kind in ("emit", "literal"):
                emitted[rule.offset] = rule.value
                steps.append((rule.offset, rule.kind, rule.value))
            elif rule.kind == "copy":
                src = rule.source_offset
                if src is None or src not in emitted:
                    raise RuleError(
                        f"rule {rule.rule_id!r} copies offset {src} which is not yet emitted"
                    )
                emitted[rule.offset] = emitted[src]
                steps.append((rule.offset, "copy", emitted[src]))
        width = max(emitted) + 1
        return [emitted.get(i, "") for i in range(width)], steps

    def walk(
        self,
        clue_id: str,
        ticket_id: str,
        *,
        crossings: Optional[dict[int, str]] = None,
    ) -> str:
        """Walk the rules to emit a whole word, reconciling shared cells.

        ``crossings`` maps an offset to a letter already fixed by a crossing
        slot; the walker must agree with every such letter.  The ticket's
        stated spelling must also match the emitted word.  A long shared prefix
        is handled naturally because ``copy`` rules transcribe earlier offsets.
        """

        if ticket_id in self._withdrawn_tickets:
            raise RuleError(f"ticket {ticket_id!r} is withdrawn")
        letters, _steps = self._emit(clue_id)
        word = "".join(letters)
        for offset, fixed in (crossings or {}).items():
            actual = word[offset] if 0 <= offset < len(word) else None
            if actual != fixed:
                raise RuleError(
                    f"{clue_id!r} offset {offset} emits {actual!r} but the crossing "
                    f"requires {fixed!r}"
                )
        ticket = self.tickets.get(ticket_id)
        if ticket is not None and ticket.clue_id == clue_id and ticket.spelling != word:
            raise RuleError(
                f"ticket {ticket_id!r} states {ticket.spelling!r} but rules emit {word!r}"
            )
        return word

    def trace(self, clue_id: str, ticket_id: str) -> tuple[tuple[int, str, str], ...]:
        """Return ``(offset, kind, value)`` steps for the emitted word."""

        if ticket_id in self._withdrawn_tickets:
            raise RuleError(f"ticket {ticket_id!r} is withdrawn")
        _letters, steps = self._emit(clue_id)
        return tuple(steps)

    # ------------------------------------------------------------------
    # offline withdrawal / replacement
    # ------------------------------------------------------------------
    def withdraw_rule(self, rule_id: str) -> None:
        self._withdrawn_rules.add(rule_id)

    def replace_rule(self, old_rule_id: str, new_rule: CharRule) -> None:
        """Replace one withdrawn rule with a new rule (same or different offset)."""

        self._withdrawn_rules.add(old_rule_id)
        self.add_rule(new_rule)

    def withdraw_ticket(self, ticket_id: str) -> None:
        self._withdrawn_tickets.add(ticket_id)


# ======================================================================
# Dispatch: pick one agreed handoff for an offline proof
# ======================================================================
def build_handoff(mode: str, **kwargs):
    """Return the chosen handoff entry point.

    ``mode`` is ``"word_form"`` or ``"rule"``; any other value is rejected so
    the editor and print shop cannot silently proceed with an unknown style.
    """

    if mode == "word_form":
        return WordFormBook.from_answer_book(**kwargs)
    if mode == "rule":
        return RuleWalker.from_tickets_and_rules(**kwargs)
    raise HandoffModeError(
        f"unknown handoff mode {mode!r}; expected 'word_form' or 'rule'"
    )

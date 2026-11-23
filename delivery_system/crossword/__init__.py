"""Bilingual crossword layer built on the two-sided delivery architecture.

The crossword feature keeps the same physical separation as the rest of
``delivery_system``:

* the **clue registry** lives on the source side and pins every sense to an
  exact ``(segment_id, revision)``;
* the **coinage repository** lives on the delivery side and binds every
  coinage sheet to an exact ``(package_id, block_id)``;
* the **answer book** joins the two sides into per-clue spellings with
  provenance;
* the **board solver** turns spellings + locks into a reopenable trial board
  (unique / ambiguous / contradictory);
* **handoff** offers the two agreed transcription styles (word-form book and
  character rules + numbered tickets) with their own entry points.

Nothing here translates text or infers meaning: roots, suffixes, and literal
characters are taken verbatim from the materials.
"""

from .answerbook import build_answer_book, resolve_clue
from .board import (
    BoardAnalysis,
    BoardDefinition,
    BoardSolution,
    Slot,
    TrialBoard,
    analyze_board,
)
from .clue_registry import PublishedClueReader, ClueRegistry
from .errors import CrosswordError, HandoffModeError
from .handoff import (
    CharRule,
    RuleWalker,
    Ticket,
    WordFormBook,
    build_handoff,
)
from .models import (
    Ban,
    ClueAnswers,
    ClueSense,
    CoinageSheet,
    Credential,
    CredentialPart,
    Root,
    SpellingSource,
    SuffixClass,
)
from .repository import CoinageRepository, open_coinage_repository

__all__ = [
    "Ban",
    "BoardAnalysis",
    "BoardDefinition",
    "BoardSolution",
    "CharRule",
    "ClueAnswers",
    "ClueRegistry",
    "ClueSense",
    "CoinageRepository",
    "CoinageSheet",
    "Credential",
    "CredentialPart",
    "CrosswordError",
    "HandoffModeError",
    "PublishedClueReader",
    "Root",
    "RuleWalker",
    "Slot",
    "SpellingSource",
    "SuffixClass",
    "Ticket",
    "TrialBoard",
    "WordFormBook",
    "analyze_board",
    "build_answer_book",
    "build_handoff",
    "open_coinage_repository",
    "resolve_clue",
]

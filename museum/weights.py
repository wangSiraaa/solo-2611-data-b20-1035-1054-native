"""Finite-domain weight CSP for the weighing room.

Each mint period (a stable catalog node id) has one unknown single-coin mass
drawn from a fixed, finite set of positive integers -- the legal masses
declared in the classification catalog.  Every coin of one period shares that
one mass.  A weighing receipt states that the plate's total net weight lies in
a closed integer interval ``[lo, hi]``; a plate holds a known number of coins
per period (counts are resolved from the ticket library).

The CSP therefore has period-mass variables and one linear interval constraint
per receipt.  It answers three questions:

1. Is there *any* mass assignment satisfying every receipt at once
   (:meth:`WeightCSP.solve` returns one concrete witness when so)?
2. Does one explicitly proposed mass table support all receipts
   (:meth:`WeightCSP.check_fixed`)?
3. When no assignment exists, which receipts already contradict one another?
   :meth:`WeightCSP.conflict_core` returns a minimal unsatisfiable subset
   (a deletion-based MUS) -- the "group of already conflicting receipts" that
   can be handed back as evidence.

The implementation is plain backtracking with constraint propagation.  The
synthetic scale is tiny (a handful of period variables and plates), so no
external solver is needed and everything runs offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


class WeightError(ValueError):
    """Rejected constraint or mass-table definition."""


@dataclass(frozen=True)
class Constraint:
    """One weighing receipt: ``lo <= sum(counts[p] * mass[p]) <= hi``."""

    receipt_id: str
    counts: Mapping[str, int]
    lo: int
    hi: int

    def weight_under(self, assignment: Mapping[str, int]) -> int | None:
        """Total weight under a (possibly partial) assignment.

        Returns ``None`` while any counted period is still unassigned.
        """

        total = 0
        for period, count in self.counts.items():
            if period not in assignment:
                return None
            total += count * assignment[period]
        return total

    def as_dict(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "counts": dict(self.counts),
            "lo": self.lo,
            "hi": self.hi,
        }


@dataclass
class WeightCSP:
    """Period-mass variables with interval constraints."""

    domains: dict[str, tuple[int, ...]] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)

    # ------------------------------------------------------------------ build
    def declare_period(self, period: str, masses: Sequence[int]) -> None:
        clean = sorted(set(masses))
        if not clean or any(
            not isinstance(m, int) or isinstance(m, bool) or m <= 0 for m in clean
        ):
            raise WeightError(
                f"period {period!r} needs at least one positive integer mass"
            )
        if period in self.domains and tuple(clean) != self.domains[period]:
            raise WeightError(f"period {period!r} already declared with another domain")
        self.domains[period] = tuple(clean)

    def add_constraint(
        self, receipt_id: str, counts: Mapping[str, int], lo: int, hi: int
    ) -> Constraint:
        if not counts or any(
            period not in self.domains for period in counts
        ):
            raise WeightError(f"receipt {receipt_id!r} counts an undeclared period")
        clean_counts = {p: int(c) for p, c in counts.items() if int(c) > 0}
        if not clean_counts:
            raise WeightError(f"receipt {receipt_id!r} has no coins on the plate")
        if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi:
            raise WeightError(f"receipt {receipt_id!r}: require lo <= hi integers")
        if lo < 0:
            raise WeightError(f"receipt {receipt_id!r}: net weight cannot be negative")
        constraint = Constraint(receipt_id, dict(sorted(clean_counts.items())), lo, hi)
        self.constraints.append(constraint)
        return constraint

    # --------------------------------------------------------------- checking
    def _violated(
        self, constraint: Constraint, assignment: Mapping[str, int]
    ) -> bool:
        weight = constraint.weight_under(assignment)
        return weight is not None and not (constraint.lo <= weight <= constraint.hi)

    def check_fixed(self, masses: Mapping[str, int]) -> list[Constraint]:
        """Return the receipts violated by one explicit mass table.

        An empty list means the proposed interpretation is fully supported.
        Every period used by a receipt must be fixed and inside its domain.
        """

        for period, mass in masses.items():
            if period in self.domains and mass not in self.domains[period]:
                raise WeightError(
                    f"mass {mass!r} is not a legal mass for period {period!r}: "
                    f"{list(self.domains[period])}"
                )
        broken: list[Constraint] = []
        for constraint in self.constraints:
            for period in constraint.counts:
                if period not in masses:
                    raise WeightError(
                        f"period {period!r} counted by {constraint.receipt_id!r} "
                        "is missing from the proposed mass table"
                    )
            if self._violated(constraint, masses):
                broken.append(constraint)
        return broken

    # ------------------------------------------------------------ backtracking
    def _variable_order(self, active: Sequence[Constraint]) -> list[str]:
        """Most-constrained variable first (ties broken by period id)."""

        touched: dict[str, int] = {}
        for constraint in active:
            for period in constraint.counts:
                touched[period] = touched.get(period, 0) + 1
        return sorted(self.domains, key=lambda p: (-touched.get(p, 0), p))

    def _iter_solutions(self, active: Sequence[Constraint]):
        """Yield every satisfying assignment as a complete mass table."""

        order = self._variable_order(active)
        assignment: dict[str, int] = {}

        def backtrack(index: int):
            if index == len(order):
                yield dict(assignment)
                return
            period = order[index]
            for mass in self.domains[period]:
                assignment[period] = mass
                if all(not self._violated(c, assignment) for c in active):
                    yield from backtrack(index + 1)
            assignment.pop(period, None)

        yield from backtrack(0)

    def solve(
        self,
        *,
        only: Sequence[Constraint] | None = None,
        want_witness: bool = True,
    ) -> dict[str, int] | None:
        """Find one deterministic mass assignment satisfying every receipt.

        ``only`` restricts the check to a subset of constraints (used by the
        conflict-core search).  The witness is the lexicographically smallest
        satisfying table (domains are sorted ascending), so the same CSP
        always yields the same established interpretation.  Returns ``None``
        when unsatisfiable; with ``want_witness=False`` an empty dict stands
        for "satisfiable" without materializing the table.
        """

        active = list(only if only is not None else self.constraints)
        for witness in self._iter_solutions(active):
            # Fill variables no active constraint touches with the smallest
            # legal mass so an empty/new CSP still yields a complete table.
            for period in self.domains:
                witness.setdefault(period, self.domains[period][0])
            if not want_witness:
                return {}
            return dict(sorted(witness.items()))
        return None

    def all_solutions(
        self, only: Sequence[Constraint] | None = None
    ) -> list[dict[str, int]]:
        """Every satisfying mass table, deterministically ordered."""

        active = list(only if only is not None else self.constraints)
        solutions = []
        for witness in self._iter_solutions(active):
            for period in self.domains:
                witness.setdefault(period, self.domains[period][0])
            solutions.append(dict(sorted(witness.items())))
        solutions.sort(key=lambda item: tuple(item[k] for k in sorted(item)))
        return solutions

    def satisfiable(self, only: Sequence[Constraint] | None = None) -> bool:
        return self.solve(only=only, want_witness=False) is not None

    # ------------------------------------------------------------ conflict core
    def conflict_core(self) -> list[Constraint]:
        """A minimal subset of receipts that is already unsatisfiable.

        Deletion-based MUS: start from all receipts and drop each one whose
        removal leaves the remainder unsatisfiable; what survives is an
        irreducible conflicting set.  Returns ``[]`` when the full CSP is
        satisfiable (there is no conflict to report).
        """

        if self.satisfiable():
            return []
        core = list(self.constraints)
        for constraint in list(core):
            remaining = [c for c in core if c is not constraint]
            # Drop the receipt when the rest alone is still contradictory;
            # keep it if removing it would make the remainder satisfiable.
            if remaining and not self.satisfiable(only=remaining):
                core = remaining
        return core

    # --------------------------------------------------------------- reporting
    def query(self, periods: Sequence[str] | None = None) -> dict:
        """Answer the weighing-room compatibility question in one report.

        ``periods`` optionally restricts which mass variables the caller cares
        about; the constraints still all have to hold.  The report either
        carries a concrete, usable counterweight table (``satisfiable`` true)
        or a named group of already conflicting receipts.
        """

        witness = self.solve()
        if witness is not None:
            if periods:
                witness = {p: witness[p] for p in periods}
            return {
                "satisfiable": True,
                "masses": witness,
                "conflicting_receipts": [],
            }
        core = self.conflict_core()
        return {
            "satisfiable": False,
            "masses": None,
            "conflicting_receipts": [c.receipt_id for c in core],
            "conflict_detail": [c.as_dict() for c in core],
        }

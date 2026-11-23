"""Minimum-ticket planner and execution-tape model.

Given the physical shuttles currently mounted on one loom, their sides and
remaining portions, a head side and a per-row color pattern, the planner
searches the reachable state space for a sequence of

* ``carry`` actions -- a shuttle porter moves one shuttle to the opposite
  side alone, costing exactly one carry ticket;
* ``pass`` actions  -- weaving one row with a shuttle on the head's current
  side; the shuttle loses one portion and both head and shuttle cross sides.
  Passes and ordinary shuttle changes cost no ticket.

Cost is lexicographic ``(tickets, actions)`` so the returned tape uses the
minimum possible number of tickets, and among those the fewest moves. The
bounds from the task (at most 6 shuttles, 16 rows, 4 tickets) keep the state
space tiny; an explicit guard rejects runaway searches instead of looping.

Only the *execution tape* variant from the task is implemented: the whole
order is compiled into concrete shuttle ids up front, the head re-verifies
each step against the physical ledger while weaving, and after a power cut
the job resumes from the last actually completed row without re-spending
thread.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass

from .errors import LoomValidationError

LEFT = "left"
RIGHT = "right"
MAX_SHUTTLES = 6
MAX_ROWS = 16
MAX_TICKETS = 4
_SEARCH_LIMIT = 200_000


def opposite(side: str) -> str:
    return RIGHT if side == LEFT else LEFT


@dataclass(frozen=True)
class ShuttleSnapshot:
    """Immutable view of a physical shuttle used for planning."""

    shuttle_id: str
    color: str
    side: str
    remaining: int

    @classmethod
    def from_row(cls, row) -> "ShuttleSnapshot":
        return cls(
            shuttle_id=row["shuttle_id"],
            color=row["color"],
            side=row["side"],
            remaining=int(row["remaining"]),
        )


@dataclass(frozen=True)
class _State:
    row_index: int          # rows [0, row_index) are already woven
    head_side: str
    positions: tuple[tuple[str, str, int], ...]  # (id, side, remaining), sorted

    def shuttle_map(self) -> dict[str, tuple[str, int]]:
        return {sid: (side, rem) for sid, side, rem in self.positions}

def validate_pattern(pattern: object) -> list[str]:
    if not isinstance(pattern, (list, tuple)) or not pattern:
        raise LoomValidationError("pattern must be a non-empty list of colors")
    if len(pattern) > MAX_ROWS:
        raise LoomValidationError(f"pattern may have at most {MAX_ROWS} rows")
    out: list[str] = []
    for color in pattern:
        if not isinstance(color, str) or not color.strip():
            raise LoomValidationError(f"pattern color must be text, got {color!r}")
        out.append(color)
    return out


def plan_tape(
    shuttles: list[ShuttleSnapshot],
    head_side: str,
    pattern: list[str],
    ticket_budget: int,
) -> dict:
    """Compile a weave order into a concrete, minimum-ticket execution tape.

    Returns ``{"feasible": True, "tickets_needed", "actions", ...}`` or a
    structured ``feasible: False`` answer explaining why (no shuttle of the
    color, empty shuttle, wrong side, ticket budget exceeded).
    """
    pattern = validate_pattern(pattern)
    if head_side not in (LEFT, RIGHT):
        raise LoomValidationError(f"head_side must be left/right, got {head_side!r}")
    if not isinstance(ticket_budget, int) or isinstance(ticket_budget, bool):
        raise LoomValidationError("ticket_budget must be an integer")
    if ticket_budget < 0 or ticket_budget > MAX_TICKETS:
        raise LoomValidationError(f"ticket budget must be within 0..{MAX_TICKETS}")
    if not shuttles:
        return _infeasible("no shuttle mounted on the loom", pattern)
    if len(shuttles) > MAX_SHUTTLES:
        raise LoomValidationError(f"a loom holds at most {MAX_SHUTTLES} shuttles")

    positions = tuple(
        sorted((s.shuttle_id, s.side, s.remaining) for s in shuttles)
    )
    start = _State(0, head_side, positions)
    goal_row = len(pattern)

    # Static impossibility: a demanded color has no mounted shuttle with thread.
    available_colors = {s.color for s in shuttles if s.remaining > 0}
    missing = [c for c in dict.fromkeys(pattern) if c not in available_colors]
    if missing:
        return _infeasible(
            "no mounted shuttle with remaining thread of color(s) "
            + ", ".join(sorted(missing)),
            pattern,
        )

    # (tickets, total_actions) ordering -> minimum tickets first.
    tie_breaker = itertools.count()
    queue: list[tuple[int, int, int, _State]] = [
        (0, 0, next(tie_breaker), start)
    ]
    best: dict[_State, tuple[int, int]] = {start: (0, 0)}
    parent: dict[_State, tuple[_State, dict]] = {}
    visited = 0

    while queue:
        tickets, actions, _, state = heapq.heappop(queue)
        if best.get(state) != (tickets, actions):
            continue
        visited += 1
        if visited > _SEARCH_LIMIT:
            raise LoomValidationError("planner search exceeded its safety bound")
        if state.row_index == goal_row:
            tape = _reconstruct(parent, state, start)
            feasible = tickets <= ticket_budget
            return {
                "feasible": feasible,
                "reason": None
                if feasible
                else f"minimum {tickets} ticket(s) needed but budget is "
                f"{ticket_budget}",
                "tickets_needed": tickets,
                "ticket_budget": ticket_budget,
                "actions": tape,
                "final_head_side": state.head_side,
                "rows": len(pattern),
                "states_explored": visited,
            }

        color = pattern[state.row_index]
        current = state.shuttle_map()

        # Moves before weaving the next row.
        for sid, (side, remaining) in current.items():
            if remaining <= 0:
                continue
            # 1) carry this shuttle to the other side (costs one ticket).
            carried = _State(
                state.row_index, state.head_side,
                _replace_position(state, sid, opposite(side), remaining),
            )
            _enqueue(
                queue, best, parent, tie_breaker, state, carried,
                tickets + 1, actions + 1,
                {
                    "kind": "carry",
                    "shuttle_id": sid,
                    "from_side": side,
                    "side": opposite(side),
                    "row_index": None,
                },
            )

            # 2) weave the row if this shuttle matches color and is head-side.
            snapshot = _snapshot(shuttles, sid)
            if side == state.head_side and snapshot.color == color:
                next_state = _State(
                    state.row_index + 1,
                    opposite(state.head_side),
                    _replace_position(state, sid, opposite(side), remaining - 1),
                )
                _enqueue(
                    queue, best, parent, tie_breaker, state, next_state,
                    tickets, actions + 1,
                    {
                        "kind": "pass",
                        "shuttle_id": sid,
                        "side": side,
                        "color": color,
                        "row_index": state.row_index,
                    },
                )

    return _infeasible(
        "pattern cannot be woven from the current shuttle positions", pattern
    )


def _enqueue(queue, best, parent, tie_breaker, prev, nxt, tickets, actions, move) -> None:
    old = best.get(nxt)
    if old is None or (tickets, actions) < old:
        best[nxt] = (tickets, actions)
        parent[nxt] = (prev, move)
        heapq.heappush(queue, (tickets, actions, next(tie_breaker), nxt))


def _replace_position(state: _State, sid: str, side: str, remaining: int):
    return tuple(
        sorted(
            (id_, side if id_ == sid else s, remaining if id_ == sid else r)
            for id_, s, r in state.positions
        )
    )


def _snapshot(shuttles: list[ShuttleSnapshot], sid: str) -> ShuttleSnapshot:
    for s in shuttles:
        if s.shuttle_id == sid:
            return s
    raise KeyError(sid)


def _reconstruct(parent: dict, goal: _State, start: _State) -> list[dict]:
    actions: list[dict] = []
    state = goal
    while state != start:
        prev, move = parent[state]
        actions.append(move)
        state = prev
    actions.reverse()
    return actions


def _infeasible(reason: str, pattern: list[str]) -> dict:
    return {
        "feasible": False,
        "reason": reason,
        "tickets_needed": None,
        "ticket_budget": None,
        "actions": [],
        "final_head_side": None,
        "rows": len(pattern),
        "states_explored": 0,
    }

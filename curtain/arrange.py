"""Sewing-order planner: keep or swap panels so every seam holds robustly.

A seam between the right edge of position ``i`` and the left edge of
position ``i + 1`` is acceptable only when *every* feasible joint
interpretation keeps the signed colour difference inside the show's
tolerance, i.e. ``max(|lo|, |hi|) <= tolerance`` for the reconciled range
``[lo, hi]``.  The planner minimises the number of swapped panels; ties may
be broken arbitrarily (here: deterministically, first-found).

The search is a dynamic program over positions with a bitmask of the spares
already consumed (a spare is a physical item and can be sewn at most once).
With the tour cap of 12 spares and 9 positions this stays tiny.
"""

from __future__ import annotations

from .errors import NoArrangement


def plan_arrangement(
    old_panels: list[dict],
    spares: list[dict],
    seam_range,
    tolerance: int,
    available: set[str] | None = None,
) -> dict:
    """Find a minimum-swap placement.

    ``old_panels`` is the ordered list of sewn panels (position 1..N).
    ``spares`` are candidate replacements with ``positions`` (1-based
    allow-list) and ``reversible`` flags.  ``seam_range(right_edge,
    left_edge)`` returns the reconciled signed range of the difference.
    ``available`` restricts which spares may be used (e.g. the truck load).
    """
    count = len(old_panels)
    usable = [
        spare
        for spare in spares
        if available is None or spare["panel_id"] in available
    ]

    # choice tuple: (spare_index | None, flipped, left_edge, right_edge, cost)
    def choices_for(position: int) -> list[tuple]:
        old = old_panels[position - 1]
        choices = [(None, False, old["left_edge"], old["right_edge"], 0)]
        for index, spare in enumerate(usable):
            if position not in spare["positions"]:
                continue
            choices.append(
                (index, False, spare["left_edge"], spare["right_edge"], 1)
            )
            if spare["reversible"]:
                choices.append(
                    (index, True, spare["right_edge"], spare["left_edge"], 1)
                )
        return choices

    position_choices = [choices_for(position) for position in range(1, count + 1)]

    seam_cache: dict[tuple, bool] = {}

    def seam_ok(right_edge: str, left_edge: str) -> bool:
        key = (right_edge, left_edge)
        ok = seam_cache.get(key)
        if ok is None:
            lo, hi = seam_range(right_edge, left_edge)
            ok = max(abs(lo), abs(hi)) <= tolerance
            seam_cache[key] = ok
        return ok

    # dp layer: (mask, choice_index) -> (cost, parent_key)
    first: dict[tuple, tuple] = {}
    for choice_index, choice in enumerate(position_choices[0]):
        spare_index = choice[0]
        mask = (1 << spare_index) if spare_index is not None else 0
        key = (mask, choice_index)
        if key not in first or choice[4] < first[key][0]:
            first[key] = (choice[4], None)
    layers = [first]

    for position in range(2, count + 1):
        previous = layers[-1]
        if not previous:
            break
        previous_choices = position_choices[position - 2]
        current: dict[tuple, tuple] = {}
        for (mask, prev_index), (cost, _parent) in previous.items():
            right_edge = previous_choices[prev_index][3]
            for choice_index, choice in enumerate(position_choices[position - 1]):
                spare_index = choice[0]
                if spare_index is not None and (mask >> spare_index) & 1:
                    continue
                if not seam_ok(right_edge, choice[2]):
                    continue
                next_mask = (
                    mask | (1 << spare_index) if spare_index is not None else mask
                )
                next_cost = cost + choice[4]
                key = (next_mask, choice_index)
                if key not in current or next_cost < current[key][0]:
                    current[key] = (next_cost, (mask, prev_index))
        layers.append(current)

    if len(layers) < count or not layers[-1]:
        raise NoArrangement(
            f"no feasible arrangement for {count} position(s) at "
            f"tolerance {tolerance}"
        )

    best_key = min(layers[-1], key=lambda key: layers[-1][key][0])
    swaps = layers[-1][best_key][0]

    # Reconstruct choices from the parent chain.
    chosen: list[int] = [0] * count
    key = best_key
    for position in range(count, 0, -1):
        mask, choice_index = key
        chosen[position - 1] = choice_index
        key = layers[position - 1][key][1]
        if key is None:
            break

    placements = []
    for position in range(1, count + 1):
        spare_index, flipped, left_edge, right_edge, _cost = position_choices[
            position - 1
        ][chosen[position - 1]]
        if spare_index is None:
            placements.append(
                {
                    "position": position,
                    "choice": "old",
                    "panel_id": old_panels[position - 1]["panel_id"],
                    "flipped": False,
                    "left_edge": left_edge,
                    "right_edge": right_edge,
                }
            )
        else:
            placements.append(
                {
                    "position": position,
                    "choice": "spare",
                    "panel_id": usable[spare_index]["panel_id"],
                    "flipped": flipped,
                    "left_edge": left_edge,
                    "right_edge": right_edge,
                }
            )
    return {"swaps": swaps, "placements": placements}


def arrangement_spares(arrangement: dict) -> list[str]:
    """Physical spare ids an arrangement consumes, in position order."""
    return [
        placement["panel_id"]
        for placement in arrangement["placements"]
        if placement["choice"] == "spare"
    ]

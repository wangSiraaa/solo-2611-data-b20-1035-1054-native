"""Shared helpers to load the offline exhibition-rights dataset.

Nothing here talks to the network; the JSON file is the whole world.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import catalog, exhibit, rights


@dataclass
class Dataset:
    raw: dict[str, Any]
    edges: list[tuple[str, str]]
    originals: dict[str, exhibit.Original]
    terms: list[rights.Term]
    tickets: dict[str, dict[str, Any]]
    use: str

    def license_for(self, ticket_id: str, edges=None) -> rights.Clearance:
        ticket = self.tickets[ticket_id]
        edge_set = list(self.edges if edges is None else edges)
        return rights.clear(
            ticket_id=ticket_id,
            direct_class=ticket["direct_class"],
            original=ticket["original"],
            use=self.use,
            edges=edge_set,
            terms=self.terms,
            holders=ticket["holders"],
        )

    def protections(self) -> dict[str, list[tuple[tuple[int, int], ...]]]:
        out: dict[str, list[tuple[tuple[int, int], ...]]] = {}
        for term in self.terms:
            out.setdefault(term.original, [])
            for group in term.protections:
                if group and group not in out[term.original]:
                    out[term.original].append(group)
        return out

    def scenario(self, scenario_id: str) -> dict[str, Any]:
        for scen in self.raw["scenarios"]:
            if scen["id"] == scenario_id:
                return scen
        raise KeyError(scenario_id)

    def label_objects(self, scenario: dict[str, Any]) -> list[exhibit.Label]:
        clearance = self.license_for(scenario["ticket"])
        out: list[exhibit.Label] = []
        for raw in scenario["labels"]:
            out.append(
                exhibit.Label(
                    id=raw["id"],
                    application_no=int(raw["app"]),
                    original=raw["blocks_original"]
                    if "blocks_original" in raw
                    else self.tickets[scenario["ticket"]]["original"],
                    compositions=[
                        exhibit.Composition.from_dict(c) for c in raw["compositions"]
                    ],
                    signature_length=int(raw["signature"]),
                    license_region=set(clearance.region),
                    panel=(int(raw["panel"][0]), int(raw["panel"][1])),
                    reserved={tuple(c) for c in raw.get("reserved", [])},
                    branch=raw.get("branch", ""),
                )
            )
        return out


def load(path: str | Path) -> Dataset:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cat = raw["catalog"]
    edges = [(str(c), str(p)) for c, p in cat["edges"]]
    originals = {
        o["id"]: exhibit.Original(o["id"], o.get("name", o["id"]), int(o["rows"]), int(o["cols"]))
        for o in raw["originals"]
    }
    terms = [rights.Term.from_dict(dict(t, use=raw["use"])) for t in raw["terms"]]
    tickets = {t["ticket_id"]: t for t in raw["tickets"]}
    return Dataset(
        raw=raw,
        edges=edges,
        originals=originals,
        terms=terms,
        tickets=tickets,
        use=raw["use"],
    )


def check_composition(
    ds: Dataset,
    label: exhibit.Label,
    comp: exhibit.Composition,
    public: dict[str, set[tuple[int, int]]],
) -> exhibit.CompositionCheck:
    return exhibit.check_composition(
        comp,
        panel=label.panel,
        reserved=label.reserved,
        signature_lengths=[label.signature_length] * len(comp.blocks),
        license_region=label.license_region,
        public_cells=public,
        protections=ds.protections(),
        original_ids=ds.originals.keys(),
    )


def initial_public(ds: Dataset, scenario: dict[str, Any]) -> dict[str, set[tuple[int, int]]]:
    out: dict[str, set[tuple[int, int]]] = {}
    for orig, cells in scenario.get("initial_public", {}).items():
        out[orig] = {(int(r), int(c)) for r, c in cells}
    return out


def batch(ds: Dataset, scenario: dict[str, Any]) -> exhibit.BatchResult:
    labels = ds.label_objects(scenario)
    return exhibit.solve_batch(
        labels,
        initial_public=initial_public(ds, scenario),
        protections=ds.protections(),
        original_ids=ds.originals.keys(),
    )

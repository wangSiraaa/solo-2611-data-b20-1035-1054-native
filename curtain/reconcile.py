"""Joint reconciliation of station offsets, reference bottles and cloth edges.

Model
-----
Every station ``s`` has a fixed integer offset ``o_s`` inside its declared
closed interval ``[lo_s, hi_s]`` (station A is declared ``[0, 0]``).  A reading
of value ``r`` with allowed error ``e`` of an object whose true colour is
``x`` on station ``s`` means::

    r - e <= x + o_s <= r + e

Objects are reference bottles (true colour ``c_b``, shared by every portion
of the same bottle) and cloth edges (true colour ``t_e``).  All true values
and all offsets share ONE joint integer interpretation; answering a seam
query by ranging each edge separately and subtracting the intervals would
miss the shared station offsets.

Reduction to difference constraints
-----------------------------------
Substituting ``ô_s = -o_s`` turns every record into binary difference
constraints, e.g. ``t_e - ô_s <= r + e`` and ``ô_s - t_e <= e - r``.  For a
feasible difference-constraint system the tightest attainable bound of
``x_a - x_b`` is exactly the shortest-path distance ``d(b, a)``, attained at
integer potentials because every weight is integral.  Hence, with ``D`` the
all-pairs shortest-path matrix::

    max(t_a - t_b) = D[b][a]
    min(t_a - t_b) = -D[a][b]

Infeasibility is a negative cycle; the cycle's edges map back to the
original readings, which is the reported conflicting core.
"""

from __future__ import annotations

INF = 10**30

ZERO = ("Z",)


def _station_node(station_id: str) -> tuple:
    return ("station", station_id)


def _bottle_node(bottle_id: str) -> tuple:
    return ("bottle", bottle_id)


def _edge_node(edge_id: str) -> tuple:
    return ("edge", edge_id)


def build_constraints(records: dict) -> list[tuple]:
    """Compile a record snapshot into ``(u, v, w, record_id)`` constraints.

    A constraint reads ``x_v - x_u <= w`` and carries the id of the original
    record it was compiled from, so a conflicting cycle can name its source
    readings.
    """
    constraints: list[tuple] = []
    for station in records.get("stations", []):
        sid = station["station_id"]
        node = _station_node(sid)
        lo = station["lo"]
        hi = station["hi"]
        # ô_s - Z <= -lo  (o_s >= lo)   and   Z - ô_s <= hi  (o_s <= hi)
        constraints.append((ZERO, node, -lo, f"station:{sid}:lo"))
        constraints.append((node, ZERO, hi, f"station:{sid}:hi"))
    for ref in records.get("references", []):
        bottle = _bottle_node(ref["bottle_id"])
        station = _station_node(ref["station_id"])
        r = ref["reading"]
        e = ref["error"]
        constraints.append((station, bottle, r + e, ref["ref_id"]))
        constraints.append((bottle, station, e - r, ref["ref_id"]))
    for reading in records.get("edge_readings", []):
        edge = _edge_node(reading["edge_id"])
        station = _station_node(reading["station_id"])
        r = reading["reading"]
        e = reading["error"]
        constraints.append((station, edge, r + e, reading["reading_id"]))
        constraints.append((edge, station, e - r, reading["reading_id"]))
    return constraints


def _nodes_of(constraints: list[tuple]) -> list[tuple]:
    nodes: list[tuple] = []
    seen: set[tuple] = set()
    for u, v, _w, _rec in constraints:
        for node in (u, v):
            if node not in seen:
                seen.add(node)
                nodes.append(node)
    return nodes


def find_conflict_core(constraints: list[tuple]) -> list[str] | None:
    """Return the record ids of one infeasible cycle, or ``None`` if feasible.

    Bellman-Ford with a virtual super-source (all distances start at 0).
    A constraint that still relaxes in the |V|-th round lies on / reaches a
    negative cycle; walking predecessors from it collects the cycle.
    """
    nodes = _nodes_of(constraints)
    if not nodes:
        return None
    dist = {node: 0 for node in nodes}
    pred: dict[tuple, tuple] = {}
    updated = None
    for _ in range(len(nodes)):
        updated = None
        for u, v, w, rec in constraints:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                pred[v] = (u, rec)
                updated = v
        if updated is None:
            return None
    # ``updated`` was still improving in the last round: it reaches a cycle.
    node = updated
    for _ in range(len(nodes)):
        node = pred[node][0]
    core: list[str] = []
    seen_records: set[str] = set()
    start = node
    while True:
        u, rec = pred[node]
        if rec not in seen_records:
            seen_records.add(rec)
            core.append(rec)
        node = u
        if node == start:
            break
    return sorted(core)


def all_pairs_shortest_paths(constraints: list[tuple]) -> dict[tuple, dict[tuple, int]]:
    """Floyd-Warshall over the constraint graph (feasible systems only)."""
    nodes = _nodes_of(constraints)
    dist: dict[tuple, dict[tuple, int]] = {
        u: {v: (0 if u == v else INF) for v in nodes} for u in nodes
    }
    for u, v, w, _rec in constraints:
        if w < dist[u][v]:
            dist[u][v] = w
    for k in nodes:
        dk = dist[k]
        for i in nodes:
            di = dist[i]
            ik = di[k]
            if ik >= INF:
                continue
            base = ik
            for j in nodes:
                cand = base + dk[j]
                if cand < di[j]:
                    di[j] = cand
    return dist


class Reconciler:
    """Feasible joint interpretation of one countersigned record snapshot."""

    def __init__(self, records: dict):
        self.records = records
        self.constraints = build_constraints(records)
        self.dist = all_pairs_shortest_paths(self.constraints)

    # -- seam queries ---------------------------------------------------------
    def difference_range(self, edge_a: str, edge_b: str) -> tuple[int, int]:
        """Signed range ``[min, max]`` of ``t_a - t_b`` over all interpretations."""
        a = _edge_node(edge_a)
        b = _edge_node(edge_b)
        if a not in self.dist or b not in self.dist:
            raise KeyError(f"unknown edge in {edge_a!r} or {edge_b!r}")
        hi = self.dist[b][a]
        lo = -self.dist[a][b]
        return lo, hi

    def witness(self, anchor_edge: str | None = None) -> dict:
        """One joint integer interpretation satisfying every record.

        With ``anchor_edge`` the witness attains the maximum of
        ``t(anchor_edge) - t(other)`` against every other edge; without an
        anchor it is the canonical potential measured from the zero anchor.
        Either way the result satisfies all records simultaneously.
        """
        if anchor_edge is None:
            source = ZERO
        else:
            source = _edge_node(anchor_edge)
        row = self.dist[source]
        zero = row[ZERO]
        return self._potential_to_witness({node: d - zero for node, d in row.items()})

    def seam_witnesses(self, edge_a: str, edge_b: str) -> dict:
        """Witnesses attaining the reported min and max of ``t_a - t_b``."""
        lo, hi = self.difference_range(edge_a, edge_b)
        witness_max = self.witness(anchor_edge=edge_b)  # max t_a - t_b = D[b][a]
        witness_min = self.witness(anchor_edge=edge_a)  # max t_b - t_a = D[a][b]
        return {
            "min": lo,
            "max": hi,
            "witness_min": witness_min,
            "witness_max": witness_max,
        }

    def _potential_to_witness(self, potential: dict) -> dict:
        witness = {"stations": {}, "bottles": {}, "edges": {}}
        for node, value in potential.items():
            kind = node[0]
            if kind == "station":
                witness["stations"][node[1]] = -value  # ô_s = -o_s
            elif kind == "bottle":
                witness["bottles"][node[1]] = value
            elif kind == "edge":
                witness["edges"][node[1]] = value
        return witness


def verify_witness(records: dict, witness: dict) -> list[str]:
    """Return the list of record ids violated by ``witness`` (empty if sound)."""
    violations: list[str] = []
    stations = witness["stations"]
    bottles = witness["bottles"]
    edges = witness["edges"]
    for station in records.get("stations", []):
        sid = station["station_id"]
        offset = stations.get(sid)
        if offset is None or not (station["lo"] <= offset <= station["hi"]):
            violations.append(f"station:{sid}")
    for ref in records.get("references", []):
        expected = bottles[ref["bottle_id"]] + stations[ref["station_id"]]
        if abs(ref["reading"] - expected) > ref["error"]:
            violations.append(ref["ref_id"])
    for reading in records.get("edge_readings", []):
        expected = edges[reading["edge_id"]] + stations[reading["station_id"]]
        if abs(reading["reading"] - expected) > reading["error"]:
            violations.append(reading["reading_id"])
    return violations

"""Freight station disposition planning.

Given the actual packing tree (up to three levels: outer cabinet -> inner
cabinet -> leaf box, with independent lead seals on each cabinet) and a notice
naming furnace batches to recall, produce the minimum-work-hours executable
plan of sealed returns, unsealing work and leaf releases.

Rules encoded here
------------------
* Any leaf that may still belong to a named batch may not be released. A leaf
  is releasable only when it carries a legal label *and* its reconstruction
  has at least one solution and none of its possible batches is named.
* A customs-locked cabinet cannot be opened: if anything inside it must stay,
  the whole cabinet is returned sealed.
* Sealed-returning a cabinet charges once and accounts for every descendant;
  descendants are never charged or listed again.
* Leaf charges only apply to individually detained leaves.
* The contract caps how many proven-clear leaves may be kept back as
  collateral (co-returned clear boxes).
* If any leaf has no legal label or no reconstructable mold segment, no plan
  is produced at all: un-verifiable facts are never treated as wholesale
  innocence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reagent.errors import ValidationError

from .matching import possible_batches

LEAF = "leaf"
CABINET = "cabinet"

RELEASE = "release"
DETAIN_LEAF = "detain_leaf"
SEAL_RETURN = "seal_return"
UNSEAL = "unseal"


@dataclass
class FreightNode:
    node_id: str
    kind: str
    customs_locked: bool = False
    label_id: str | None = None
    children: list["FreightNode"] = field(default_factory=list)
    parent: "FreightNode | None" = None


@dataclass(frozen=True)
class CostModel:
    sealed_return: dict[str, int]      # node_id -> work hours for that cabinet
    unseal: dict[str, int]             # node_id -> break-seal work hours
    leaf_charge: int                   # per individually detained leaf


@dataclass
class LeafFact:
    label_id: str
    legal: bool
    resolvable: bool
    possible_batches: tuple[str, ...]
    must_hold: bool                    # may belong to a named batch


@dataclass
class PlanAction:
    action: str
    node_id: str
    cost: int
    labels: tuple[str, ...]
    detail: str

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "node_id": self.node_id,
            "cost": self.cost,
            "labels": list(self.labels),
            "detail": self.detail,
        }


class PlanBlocked(Exception):
    """Raised when an illegal/un-verifiable leaf forbids producing any plan."""

    def __init__(self, blockers: list[dict]):
        self.blockers = blockers
        super().__init__(f"plan blocked by {len(blockers)} leaf/leaves")


# ---------------------------------------------------------------------------
# tree construction and validation
# ---------------------------------------------------------------------------
def build_tree(
    nodes: list[dict], max_depth: int = 2
) -> tuple[FreightNode, dict[str, FreightNode]]:
    by_id: dict[str, FreightNode] = {}
    for doc in nodes:
        node = FreightNode(
            node_id=doc["node_id"],
            kind=doc["kind"],
            customs_locked=bool(doc.get("customs_locked", False)),
            label_id=doc.get("label_id"),
        )
        if node.kind not in (CABINET, LEAF):
            raise ValidationError(f"unknown node kind {node.kind!r}")
        if node.kind == LEAF and node.children:
            raise ValidationError("leaf node cannot have children")
        if node.kind == LEAF and not node.label_id:
            raise ValidationError(f"leaf {node.node_id} must carry a label")
        by_id[node.node_id] = node
    roots: list[FreightNode] = []
    for doc in nodes:
        parent_id = doc.get("parent_id")
        node = by_id[doc["node_id"]]
        if parent_id is None:
            roots.append(node)
            continue
        if parent_id not in by_id:
            raise ValidationError(
                f"node {node.node_id} references unknown parent {parent_id!r}"
            )
        parent = by_id[parent_id]
        if parent.kind != CABINET:
            raise ValidationError(f"parent of {node.node_id} must be a cabinet")
        parent.children.append(node)
        node.parent = parent
    if len(roots) != 1:
        raise ValidationError("freight tree must have exactly one root")
    _validate_depth(roots[0], 0, max_depth)
    return roots[0], by_id


def _validate_depth(node: FreightNode, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise ValidationError(f"node {node.node_id} exceeds the 3-level limit")
    if node.kind == LEAF and depth == 0:
        raise ValidationError("the root must be a cabinet")
    for child in node.children:
        _validate_depth(child, depth + 1, max_depth)


def iter_leaves(node: FreightNode):
    if node.kind == LEAF:
        yield node
        return
    for child in node.children:
        yield from iter_leaves(child)


# ---------------------------------------------------------------------------
# leaf classification from archive reconstructions
# ---------------------------------------------------------------------------
def classify_leaves(
    root: FreightNode,
    archive,
    named_batches: frozenset[str],
) -> dict[str, LeafFact]:
    facts: dict[str, LeafFact] = {}
    blockers: list[dict] = []
    for leaf in iter_leaves(root):
        label_id = leaf.label_id
        legal = archive.conn.execute(
            "SELECT 1 FROM labels WHERE label_id = ?", (label_id,)
        ).fetchone() is not None
        if not legal:
            blockers.append(
                {
                    "node_id": leaf.node_id,
                    "label_id": label_id,
                    "reason": "no legal seal-label issuance exists",
                }
            )
            facts[leaf.node_id] = LeafFact(label_id, False, False, (), True)
            continue
        imprint_rows = archive.conn.execute(
            "SELECT * FROM imprint_versions WHERE label_id = ? "
            "ORDER BY version DESC",
            (label_id,),
        ).fetchall()
        if not imprint_rows:
            blockers.append(
                {
                    "node_id": leaf.node_id,
                    "label_id": label_id,
                    "reason": "label has no archived imprint read",
                }
            )
            facts[leaf.node_id] = LeafFact(label_id, True, False, (), True)
            continue
        row = imprint_rows[0]
        imprint = archive._imprint_from_row(row)
        # use the pure engine directly so classification does not mutate tables
        from .matching import reconstruct

        solutions, _failures = reconstruct(
            imprint, archive._load_molds(), archive._load_vouchers()
        )
        if not solutions:
            blockers.append(
                {
                    "node_id": leaf.node_id,
                    "label_id": label_id,
                    "reason": "no mold segment can be reconstructed for the imprint",
                }
            )
            facts[leaf.node_id] = LeafFact(label_id, True, False, (), True)
            continue
        batches = tuple(possible_batches(solutions))
        must_hold = any(b in named_batches for b in batches)
        facts[leaf.node_id] = LeafFact(
            label_id, True, True, batches, must_hold
        )
    if blockers:
        raise PlanBlocked(blockers)
    return facts


# ---------------------------------------------------------------------------
# minimum-cost plan via exhaustive search (trees are tiny: <= 20 labels)
# ---------------------------------------------------------------------------
@dataclass
class _Choice:
    cost: int
    sealed: bool                 # this whole subtree is returned sealed
    clear_co_returned: int       # proven-clear leaves kept inside sealed returns
    actions: list[PlanAction]
    covered_labels: frozenset[str]


def _subtree_leaves(node: FreightNode) -> list[FreightNode]:
    return list(iter_leaves(node))


def _solve(
    node: FreightNode,
    facts: dict[str, LeafFact],
    costs: CostModel,
) -> list[_Choice]:
    if node.kind == LEAF:
        fact = facts[node.node_id]
        if fact.must_hold:
            return [
                _Choice(
                    cost=costs.leaf_charge,
                    sealed=False,
                    clear_co_returned=0,
                    actions=[
                        PlanAction(
                            DETAIN_LEAF,
                            node.node_id,
                            costs.leaf_charge,
                            (fact.label_id,),
                            "leaf may belong to a recalled batch",
                        )
                    ],
                    covered_labels=frozenset({fact.label_id}),
                )
            ]
        # proven-clear leaf: release, or (later) ride inside a sealed ancestor
        return [
            _Choice(
                cost=0,
                sealed=False,
                clear_co_returned=0,
                actions=[
                    PlanAction(
                        RELEASE,
                        node.node_id,
                        0,
                        (fact.label_id,),
                        "proven clear: released to customer",
                    )
                ],
                covered_labels=frozenset({fact.label_id}),
            )
        ]

    leaf_facts = [facts[leaf.node_id] for leaf in _subtree_leaves(node)]
    any_hold = any(f.must_hold for f in leaf_facts)
    clear_count = sum(1 for f in leaf_facts if not f.must_hold)
    labels = frozenset(f.label_id for f in leaf_facts)

    choices: list[_Choice] = []

    # option A: sealed return of the whole cabinet (legal whenever something
    # inside must be held; customs lock makes it mandatory)
    if any_hold:
        sealed_cost = costs.sealed_return[node.node_id]
        choices.append(
            _Choice(
                cost=sealed_cost,
                sealed=True,
                clear_co_returned=clear_count,
                actions=[
                    PlanAction(
                        SEAL_RETURN,
                        node.node_id,
                        sealed_cost,
                        tuple(sorted(labels)),
                        f"cabinet returned sealed with all {len(labels)} descendant(s)",
                    )
                ],
                covered_labels=labels,
            )
        )

    # option B: open the cabinet and solve each child independently.
    # impossible while customs-locked, and pointless when nothing must hold.
    if any_hold and not node.customs_locked:
        unseal_cost = costs.unseal[node.node_id]
        child_choice_sets = [
            _solve(child, facts, costs) for child in node.children
        ]
        for combo in _cartesian(child_choice_sets):
            actions = [
                PlanAction(
                    UNSEAL,
                    node.node_id,
                    unseal_cost,
                    (),
                    "break the cabinet's lead seal and handle children",
                )
            ]
            total = unseal_cost
            co_returned = 0
            covered: set[str] = set()
            for choice in combo:
                total += choice.cost
                co_returned += choice.clear_co_returned
                actions.extend(choice.actions)
                covered.update(choice.covered_labels)
            choices.append(
                _Choice(
                    cost=total,
                    sealed=False,
                    clear_co_returned=co_returned,
                    actions=actions,
                    covered_labels=frozenset(covered),
                )
            )
    return choices


def _cartesian(option_sets: list[list[_Choice]]) -> list[list[_Choice]]:
    if not option_sets:
        return [[]]
    rest = _cartesian(option_sets[1:])
    out: list[list[_Choice]] = []
    for head in option_sets[0]:
        for tail in rest:
            out.append([head, *tail])
    return out


def plan_dispositions(
    root: FreightNode,
    facts: dict[str, LeafFact],
    costs: CostModel,
    max_co_returned_clear: int,
    trucks: tuple[str, ...] = ("truck-1", "truck-2"),
) -> dict:
    choices = _solve(root, facts, costs)
    feasible = [c for c in choices if c.clear_co_returned <= max_co_returned_clear]
    if not feasible:
        limit = max_co_returned_clear
        raise ValidationError(
            f"no executable plan keeps at most {limit} proven-clear co-returned "
            "leaf/leaves"
        )
    best = min(feasible, key=lambda c: (c.cost, len(c.actions)))
    # sanity: every leaf appears exactly once
    all_labels = {f.label_id for f in facts.values()}
    if best.covered_labels != all_labels:
        raise ValidationError("internal error: plan does not partition the leaves")
    return {
        "total_work_hours": best.cost,
        "co_returned_clear": best.clear_co_returned,
        "co_return_limit": max_co_returned_clear,
        "trucks": list(trucks),
        "actions": [a.as_dict() for a in best.actions],
        "leaf_outcomes": _leaf_outcomes(root, facts, best),
    }


def _leaf_outcomes(
    root: FreightNode, facts: dict[str, LeafFact], choice: _Choice
) -> list[dict]:
    # map each leaf to the action that actually accounts for it
    sealed_nodes = {
        a.node_id for a in choice.actions if a.action == SEAL_RETURN
    }
    out: list[dict] = []
    for leaf in iter_leaves(root):
        fact = facts[leaf.node_id]
        ancestor = _find_sealed_ancestor(leaf, sealed_nodes)
        if ancestor is not None:
            action = f"inside sealed return {ancestor}"
        elif fact.must_hold:
            action = "individually detained"
        else:
            action = "released to customer"
        out.append(
            {
                "node_id": leaf.node_id,
                "label_id": fact.label_id,
                "possible_batches": list(fact.possible_batches),
                "outcome": action,
            }
        )
    return out


def _find_sealed_ancestor(
    leaf: FreightNode, sealed_nodes: set[str]
) -> str | None:
    node = leaf.parent
    while node is not None:
        if node.node_id in sealed_nodes:
            return node.node_id
        node = node.parent
    return None

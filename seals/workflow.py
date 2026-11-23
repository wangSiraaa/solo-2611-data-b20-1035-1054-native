"""Persistent freight workflow engine.

Two operating modes are supported; the exercised one is ``cabinet_first``::

    lock whole cabinet -> librarian breaks seals layer by layer against the
    approved plan -> every responsibility still on hand is transferred to a
    sub-cabinet / leaf -> dispositions are recorded once.

Every state transition is committed to SQLite *before* the caller is handed a
return packet. If the packet is lost (a crash, a dead scanner after breaking a
seal), reopening the workflow reconstructs the truth from the stored rows:

* a cabinet whose seal is already broken is recognized from its break receipt
  and never broken twice;
* a leaf already dispositioned is never dispositioned again, so one box can
  never both ship to the customer and hang on a return cart;
* the remaining to-do actions are derived from the plan minus the executed
  ones.
"""

from __future__ import annotations

import json
import sqlite3

from reagent.errors import Conflict, NotFoundError, ValidationError
from reagent.rules import advance_day
from reagent.schema import transaction

from . import freight
from .freight import (
    CABINET,
    DETAIN_LEAF,
    LEAF,
    RELEASE,
    SEAL_RETURN,
    UNSEAL,
    FreightNode,
)

CABINET_FIRST = "cabinet_first"
BOX_FIRST = "box_first"


class WorkflowService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- packing tree -------------------------------------------------------
    def register_tree(self, nodes: list[dict]) -> dict:
        root, by_id = freight.build_tree(nodes)
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM freight_nodes LIMIT 1"
            ).fetchone():
                raise Conflict("a freight tree is already registered")
            # parents first so the FK is satisfiable
            ordered = _topological(nodes, by_id)
            for node_id in ordered:
                node = by_id[node_id]
                self.conn.execute(
                    "INSERT INTO freight_nodes(node_id, parent_id, kind, depth, "
                    "customs_locked, label_id) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        node.node_id,
                        node.parent.node_id if node.parent else None,
                        node.kind,
                        _depth(node),
                        1 if node.customs_locked else 0,
                        node.label_id,
                    ),
                )
        return self.tree_view()

    def tree_view(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM freight_nodes ORDER BY depth, node_id"
        ).fetchall()
        return {
            "nodes": [
                {
                    "node_id": r["node_id"],
                    "parent_id": r["parent_id"],
                    "kind": r["kind"],
                    "depth": r["depth"],
                    "customs_locked": bool(r["customs_locked"]),
                    "label_id": r["label_id"],
                }
                for r in rows
            ]
        }

    def load_tree(self) -> FreightNode:
        docs = [
            {
                "node_id": r["node_id"],
                "parent_id": r["parent_id"],
                "kind": r["kind"],
                "customs_locked": bool(r["customs_locked"]),
                "label_id": r["label_id"],
            }
            for r in self.conn.execute(
                "SELECT * FROM freight_nodes ORDER BY depth, node_id"
            )
        ]
        if not docs:
            raise NotFoundError("no freight tree registered")
        root, _ = freight.build_tree(docs)
        return root

    # -- workflow lifecycle -------------------------------------------------
    def open_workflow(self, workflow_id: str, day: int, mode: str = CABINET_FIRST) -> dict:
        if mode not in (CABINET_FIRST, BOX_FIRST):
            raise ValidationError(f"unknown workflow mode {mode!r}")
        with transaction(self.conn):
            advance_day(self.conn, day)
            if self.conn.execute(
                "SELECT 1 FROM freight_workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone():
                raise Conflict(f"workflow {workflow_id!r} already exists")
            self.conn.execute(
                "INSERT INTO freight_workflows(workflow_id, mode, state, created_day) "
                "VALUES(?, ?, 'open', ?)",
                (workflow_id, mode, day),
            )
            return self.get_workflow(workflow_id)

    def close_workflow(self, workflow_id: str, day: int) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            wf = self._require_workflow(workflow_id)
            if wf["state"] != "open":
                raise Conflict(
                    f"workflow {workflow_id} is {wf['state']}, cannot close"
                )
            self.conn.execute(
                "UPDATE freight_workflows SET state = 'closed', closed_day = ? "
                "WHERE workflow_id = ?",
                (day, workflow_id),
            )
            return self.get_workflow(workflow_id)

    def reopen_workflow(self, workflow_id: str, day: int) -> dict:
        """Reopen after a lost packet: recognize broken seals, shipped boxes
        and the remaining to-do actions from persisted rows."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            wf = self._require_workflow(workflow_id)
            if wf["state"] != "closed":
                raise Conflict(
                    f"workflow {workflow_id} is {wf['state']}; only a closed "
                    "workflow can be reopened"
                )
            self.conn.execute(
                "UPDATE freight_workflows SET state = 'open', closed_day = NULL "
                "WHERE workflow_id = ?",
                (workflow_id,),
            )
            return self.recovery_state(workflow_id)

    def finalize_workflow(self, workflow_id: str, day: int) -> dict:
        with transaction(self.conn):
            advance_day(self.conn, day)
            plan = self._require_plan(workflow_id)
            pending = self.pending_actions(workflow_id, plan)
            if pending:
                raise Conflict(
                    f"workflow {workflow_id} still has {len(pending)} pending "
                    "actions; cannot finalize"
                )
            self.conn.execute(
                "UPDATE freight_workflows SET state = 'finalized', finalized_day = ? "
                "WHERE workflow_id = ?",
                (day, workflow_id),
            )
            return self.get_workflow(workflow_id)

    # -- plan approval ------------------------------------------------------
    def approve_plan(self, workflow_id: str, plan: dict) -> dict:
        with transaction(self.conn):
            wf = self._require_workflow(workflow_id)
            if wf["state"] != "open":
                raise Conflict("only an open workflow can hold a plan")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (f"plan:{workflow_id}", json.dumps(plan, sort_keys=True)),
            )
            return plan

    def get_plan(self, workflow_id: str) -> dict:
        return self._require_plan(workflow_id)

    # -- cabinet_first execution -------------------------------------------
    def lock_cabinet(self, workflow_id: str, node_id: str, day: int) -> dict:
        """Step 1: lock the whole cabinet intact before any seal is touched."""
        with transaction(self.conn):
            self._require_open(workflow_id)
            advance_day(self.conn, day)
            node = self._require_node(node_id)
            if node["kind"] != CABINET:
                raise ValidationError(f"{node_id} is not a cabinet")
            existing = self._lock_row(workflow_id, node_id)
            if existing is not None:
                return self._lock_view(existing)
            lock_id = f"{workflow_id}:{node_id}"
            self.conn.execute(
                "INSERT INTO cabinet_locks(lock_id, workflow_id, node_id, state, "
                "locked_day) VALUES(?, ?, ?, 'locked_intact', ?)",
                (lock_id, workflow_id, node_id, day),
            )
            return self._lock_view(self._lock_row(workflow_id, node_id))

    def break_seal(
        self, workflow_id: str, node_id: str, day: int, receipt: dict
    ) -> dict:
        """Step 2: break a cabinet seal. The receipt is persisted first, so a
        lost return packet cannot make the same seal breakable twice."""
        with transaction(self.conn):
            self._require_open(workflow_id)
            advance_day(self.conn, day)
            lock = self._require_lock(workflow_id, node_id)
            if lock["state"] in ("broken", "sealed_returned"):
                # idempotent replay: return the stored truth, never re-break
                return self._lock_view(lock, replayed=True)
            if not isinstance(receipt, dict) or not receipt:
                raise ValidationError("a break receipt must be a non-empty object")
            receipt = dict(receipt)
            receipt.setdefault("workflow_id", workflow_id)
            receipt.setdefault("node_id", node_id)
            receipt["broken_day"] = day
            self.conn.execute(
                "UPDATE cabinet_locks SET state = 'broken', broken_day = ?, "
                "break_receipt = ? WHERE lock_id = ?",
                (day, json.dumps(receipt, sort_keys=True), lock["lock_id"]),
            )
            return self._lock_view(
                self._lock_row(workflow_id, node_id), replayed=False
            )

    def execute_action(
        self, workflow_id: str, action: dict, day: int
    ) -> dict:
        """Execute one approved plan action with full idempotency."""
        with transaction(self.conn):
            self._require_open(workflow_id)
            advance_day(self.conn, day)
            name = action["action"]
            node_id = action["node_id"]
            if name == SEAL_RETURN:
                return self._seal_return(workflow_id, node_id, day)
            if name == UNSEAL:
                return self.break_seal(
                    workflow_id,
                    node_id,
                    day,
                    {"planned": True, "reason": action.get("detail", "plan")},
                )
            if name in (DETAIN_LEAF, RELEASE):
                return self._dispose_leaf(workflow_id, node_id, day, name)
            raise ValidationError(f"unknown plan action {name!r}")

    def _seal_return(self, workflow_id: str, node_id: str, day: int) -> dict:
        lock = self._lock_row(workflow_id, node_id)
        if lock is None:
            # the plan may return a cabinet that was never explicitly locked:
            # lock it intact first, then return it sealed without opening
            self.lock_cabinet(workflow_id, node_id, day)
            lock = self._lock_row(workflow_id, node_id)
        if lock["state"] == "sealed_returned":
            return self._lock_view(lock, replayed=True)
        if lock["state"] == "broken":
            raise Conflict(
                f"cabinet {node_id} was already broken; it cannot be returned "
                "under its original seal"
            )
        labels = self._leaf_labels_under(node_id)
        # every descendant is accounted for by this one return
        for label_id in labels:
            self._insert_disposition(
                workflow_id, label_id, "detained",
                "inside_sealed_cabinet", lock["lock_id"], day
            )
        self.conn.execute(
            "UPDATE cabinet_locks SET state = 'sealed_returned', returned_day = ? "
            "WHERE lock_id = ?",
            (day, lock["lock_id"]),
        )
        return self._lock_view(self._lock_row(workflow_id, node_id))

    def _dispose_leaf(
        self, workflow_id: str, node_id: str, day: int, name: str
    ) -> dict:
        node = self._require_node(node_id)
        if node["kind"] != LEAF:
            raise ValidationError(f"{node_id} is not a leaf box")
        label_id = node["label_id"]
        # a leaf inside a sealed-returned ancestor is already accounted for;
        # check this before the idempotent replay so a conflicting *different*
        # action is refused instead of silently replaying the return's hold
        ancestor = self._sealed_ancestor(node_id)
        if ancestor is not None:
            raise Conflict(
                f"leaf {node_id} is already covered by sealed return {ancestor}"
            )
        existing = self.conn.execute(
            "SELECT * FROM leaf_dispositions WHERE workflow_id = ? AND label_id = ?",
            (workflow_id, label_id),
        ).fetchone()
        if existing is not None:
            return {
                "replayed": True,
                "disposition_id": existing["disposition_id"],
                "label_id": label_id,
                "action": existing["action"],
                "via": existing["via"],
            }
        action = "released" if name == RELEASE else "detained"
        via = "leaf_charge"
        lock = self._nearest_broken_lock(workflow_id, node_id)
        if lock is not None:
            via = "inside_opened_cabinet"
        self._insert_disposition(
            workflow_id, label_id, action, via,
            lock["lock_id"] if lock else None, day
        )
        return {
            "replayed": False,
            "label_id": label_id,
            "action": action,
            "via": via,
        }

    # -- recovery / pending derivation --------------------------------------
    def recovery_state(self, workflow_id: str) -> dict:
        """What a reopen must recognize without redoing anything."""
        wf = self._require_workflow(workflow_id)
        locks = [
            self._lock_view(r)
            for r in self.conn.execute(
                "SELECT * FROM cabinet_locks WHERE workflow_id = ? ORDER BY node_id",
                (workflow_id,),
            )
        ]
        dispositions = [
            {
                "label_id": r["label_id"],
                "action": r["action"],
                "via": r["via"],
                "day": r["day"],
            }
            for r in self.conn.execute(
                "SELECT * FROM leaf_dispositions WHERE workflow_id = ? "
                "ORDER BY label_id",
                (workflow_id,),
            )
        ]
        plan = self._plan_or_none(workflow_id)
        pending = (
            self.pending_actions(workflow_id, plan) if plan is not None else []
        )
        return {
            "workflow_id": workflow_id,
            "state": wf["state"],
            "broken_seals": [
                l for l in locks if l["state"] == "broken"
            ],
            "sealed_returns": [
                l for l in locks if l["state"] == "sealed_returned"
            ],
            "dispositions": dispositions,
            "pending_actions": pending,
        }

    def pending_actions(self, workflow_id: str, plan: dict) -> list[dict]:
        """Approved plan actions not yet backed by a persisted fact."""
        done_labels = {
            r["label_id"]
            for r in self.conn.execute(
                "SELECT label_id FROM leaf_dispositions WHERE workflow_id = ?",
                (workflow_id,),
            )
        }
        pending: list[dict] = []
        for action in plan["actions"]:
            name = action["action"]
            node_id = action["node_id"]
            if name == SEAL_RETURN:
                lock = self._lock_row(workflow_id, node_id)
                if lock is None or lock["state"] != "sealed_returned":
                    pending.append(action)
            elif name == UNSEAL:
                lock = self._lock_row(workflow_id, node_id)
                if lock is None or lock["state"] not in ("broken", "sealed_returned"):
                    pending.append(action)
            else:
                node = self._require_node(node_id)
                if node["label_id"] not in done_labels:
                    pending.append(action)
        return pending

    def get_workflow(self, workflow_id: str) -> dict:
        wf = self._require_workflow(workflow_id)
        return {k: wf[k] for k in wf.keys()}

    # -- box_first merge primitives (second operating mode) -----------------
    def register_box_hold(
        self, workflow_id: str, label_id: str, day: int
    ) -> dict:
        with transaction(self.conn):
            self._require_open(workflow_id, mode=BOX_FIRST)
            advance_day(self.conn, day)
            if self.conn.execute(
                "SELECT 1 FROM labels WHERE label_id = ?", (label_id,)
            ).fetchone() is None:
                raise NotFoundError(f"unknown legal label {label_id!r}")
            hold_id = f"{workflow_id}:hold:{label_id}"
            if self.conn.execute(
                "SELECT 1 FROM box_holds WHERE hold_id = ?", (hold_id,)
            ).fetchone():
                raise Conflict(f"hold for {label_id} already registered")
            self.conn.execute(
                "INSERT INTO box_holds(hold_id, workflow_id, label_id, state, "
                "created_day) VALUES(?, ?, ?, 'held', ?)",
                (hold_id, workflow_id, label_id, day),
            )
            return {"hold_id": hold_id, "state": "held"}

    def merge_holds(
        self, workflow_id: str, node_id: str, day: int
    ) -> dict:
        """Merge still-pending box holds into a cabinet that may be returned
        sealed. The merge is only *pending* until the seal is confirmed."""
        with transaction(self.conn):
            self._require_open(workflow_id, mode=BOX_FIRST)
            advance_day(self.conn, day)
            merge_id = f"{workflow_id}:merge:{node_id}"
            existing = self.conn.execute(
                "SELECT state FROM cabinet_merges WHERE merge_id = ?", (merge_id,)
            ).fetchone()
            if existing is not None and existing["state"] != "withdrawn":
                raise Conflict(f"merge for {node_id} already exists")
            holds = self._holds_under(workflow_id, node_id)
            if not holds:
                raise ValidationError(f"no pending holds under {node_id}")
            if existing is not None:
                # the withdrawn row stays on file; the retry gets a distinct
                # merge id so both attempts remain auditable
                suffix = 2
                candidate = f"{merge_id}~{suffix}"
                while self.conn.execute(
                    "SELECT 1 FROM cabinet_merges WHERE merge_id = ?", (candidate,)
                ).fetchone():
                    suffix += 1
                    candidate = f"{merge_id}~{suffix}"
                merge_id = candidate
            self.conn.execute(
                "INSERT INTO cabinet_merges(merge_id, workflow_id, node_id, state, "
                "created_day) VALUES(?, ?, ?, 'pending', ?)",
                (merge_id, workflow_id, node_id, day),
            )
            return {
                "merge_id": merge_id,
                "state": "pending",
                "holds": [r["hold_id"] for r in holds],
            }

    def confirm_merge_sealed(
        self, workflow_id: str, node_id: str, day: int
    ) -> dict:
        with transaction(self.conn):
            self._require_open(workflow_id, mode=BOX_FIRST)
            advance_day(self.conn, day)
            merge = self._require_merge(workflow_id, node_id)
            if merge["state"] != "pending":
                raise Conflict(f"merge {node_id} is {merge['state']}")
            lock_id = f"{workflow_id}:{node_id}"
            self.conn.execute(
                "INSERT INTO cabinet_locks(lock_id, workflow_id, node_id, state, "
                "locked_day, returned_day) "
                "VALUES(?, ?, ?, 'sealed_returned', ?, ?) "
                "ON CONFLICT(lock_id) DO UPDATE SET state = 'sealed_returned', "
                "returned_day = excluded.returned_day",
                (lock_id, workflow_id, node_id, day, day),
            )
            for hold in self._holds_under(workflow_id, node_id):
                self.conn.execute(
                    "UPDATE box_holds SET state = 'merged', merged_into = ? "
                    "WHERE hold_id = ?",
                    (lock_id, hold["hold_id"]),
                )
                self._insert_disposition(
                    workflow_id, hold["label_id"], "detained",
                    "inside_sealed_cabinet", lock_id, day
                )
            self.conn.execute(
                "UPDATE cabinet_merges SET state = 'sealed_returned', sealed_day = ? "
                "WHERE merge_id = ?",
                (day, merge["merge_id"]),
            )
            return {"merge_id": merge["merge_id"], "state": "sealed_returned"}

    def withdraw_merge(
        self, workflow_id: str, node_id: str, day: int
    ) -> dict:
        """Withdraw a merge that was never sealed (e.g. scanner died): the
        box holds return to 'held' and any already-shipped box stays shipped."""
        with transaction(self.conn):
            self._require_open(workflow_id, mode=BOX_FIRST)
            advance_day(self.conn, day)
            merge = self._require_merge(workflow_id, node_id)
            if merge["state"] != "pending":
                raise Conflict(
                    f"a {merge['state']} merge cannot be withdrawn"
                )
            for hold in self._holds_under(workflow_id, node_id):
                self.conn.execute(
                    "UPDATE box_holds SET state = 'held', merged_into = NULL "
                    "WHERE hold_id = ?",
                    (hold["hold_id"],),
                )
            self.conn.execute(
                "UPDATE cabinet_merges SET state = 'withdrawn', withdrawn_day = ? "
                "WHERE merge_id = ?",
                (day, merge["merge_id"]),
            )
            return {"merge_id": merge["merge_id"], "state": "withdrawn"}

    # -- internal helpers ---------------------------------------------------
    def _insert_disposition(
        self, workflow_id, label_id, action, via, lock_id, day
    ) -> None:
        disposition_id = f"{workflow_id}:disp:{label_id}"
        self.conn.execute(
            "INSERT INTO leaf_dispositions(disposition_id, workflow_id, label_id, "
            "action, via, lock_id, day) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workflow_id, label_id) DO NOTHING",
            (disposition_id, workflow_id, label_id, action, via, lock_id, day),
        )

    def _leaf_labels_under(self, node_id: str) -> list[str]:
        return [
            r["label_id"]
            for r in self.conn.execute(
                """
                WITH RECURSIVE sub(node_id) AS (
                    SELECT ?
                    UNION ALL
                    SELECT n.node_id FROM freight_nodes n JOIN sub ON n.parent_id = sub.node_id
                )
                SELECT label_id FROM freight_nodes
                 WHERE node_id IN (SELECT node_id FROM sub) AND kind = 'leaf'
                 ORDER BY node_id
                """,
                (node_id,),
            )
        ]

    def _holds_under(self, workflow_id: str, node_id: str) -> list[sqlite3.Row]:
        labels = self._leaf_labels_under(node_id)
        if not labels:
            return []
        placeholders = ",".join("?" for _ in labels)
        return self.conn.execute(
            f"SELECT * FROM box_holds WHERE workflow_id = ? AND state = 'held' "
            f"AND label_id IN ({placeholders}) ORDER BY label_id",
            (workflow_id, *labels),
        ).fetchall()

    def _sealed_ancestor(self, node_id: str) -> str | None:
        rows = self.conn.execute(
            """
            WITH RECURSIVE anc(node_id, parent_id) AS (
                SELECT node_id, parent_id FROM freight_nodes WHERE node_id = ?
                UNION ALL
                SELECT n.node_id, n.parent_id FROM freight_nodes n
                  JOIN anc ON n.node_id = anc.parent_id
            )
            SELECT cl.node_id FROM cabinet_locks cl
              JOIN anc ON anc.node_id = cl.node_id
             WHERE cl.state = 'sealed_returned' LIMIT 1
            """,
            (node_id,),
        ).fetchall()
        return rows[0]["node_id"] if rows else None

    def _nearest_broken_lock(
        self, workflow_id: str, node_id: str
    ) -> sqlite3.Row | None:
        rows = self.conn.execute(
            """
            WITH RECURSIVE anc(node_id, parent_id, depth) AS (
                SELECT node_id, parent_id, 0 FROM freight_nodes WHERE node_id = ?
                UNION ALL
                SELECT n.node_id, n.parent_id, anc.depth + 1 FROM freight_nodes n
                  JOIN anc ON n.node_id = anc.parent_id
            )
            SELECT cl.* FROM cabinet_locks cl
              JOIN anc ON anc.node_id = cl.node_id
             WHERE cl.workflow_id = ? AND cl.state = 'broken'
             ORDER BY anc.depth LIMIT 1
            """,
            (node_id, workflow_id),
        ).fetchall()
        return rows[0] if rows else None

    def _require_open(self, workflow_id: str, mode: str | None = None) -> sqlite3.Row:
        wf = self._require_workflow(workflow_id)
        if wf["state"] != "open":
            raise Conflict(f"workflow {workflow_id} is {wf['state']}, not open")
        if mode is not None and wf["mode"] != mode:
            raise Conflict(
                f"workflow {workflow_id} runs in {wf['mode']} mode, not {mode}"
            )
        return wf

    def _require_workflow(self, workflow_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM freight_workflows WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown workflow {workflow_id!r}")
        return row

    def _require_node(self, node_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM freight_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown freight node {node_id!r}")
        return row

    def _lock_row(self, workflow_id: str, node_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM cabinet_locks WHERE workflow_id = ? AND node_id = ?",
            (workflow_id, node_id),
        ).fetchone()

    def _require_lock(self, workflow_id: str, node_id: str) -> sqlite3.Row:
        row = self._lock_row(workflow_id, node_id)
        if row is None:
            raise Conflict(f"cabinet {node_id} has not been locked yet")
        return row

    def _require_merge(self, workflow_id: str, node_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM cabinet_merges WHERE workflow_id = ? AND node_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (workflow_id, node_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no merge registered for cabinet {node_id!r}")
        return row

    def _require_plan(self, workflow_id: str) -> dict:
        plan = self._plan_or_none(workflow_id)
        if plan is None:
            raise NotFoundError(f"workflow {workflow_id} has no approved plan")
        return plan

    def _plan_or_none(self, workflow_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"plan:{workflow_id}",)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def _lock_view(
        self, row: sqlite3.Row, replayed: bool = False
    ) -> dict:
        out = {
            "lock_id": row["lock_id"],
            "node_id": row["node_id"],
            "state": row["state"],
            "locked_day": row["locked_day"],
            "broken_day": row["broken_day"],
            "returned_day": row["returned_day"],
            "break_receipt": json.loads(row["break_receipt"])
            if row["break_receipt"]
            else None,
        }
        if replayed:
            out["replayed"] = True
        return out


def _depth(node: FreightNode) -> int:
    depth = 0
    while node.parent is not None:
        depth += 1
        node = node.parent
    return depth


def _topological(nodes: list[dict], by_id: dict[str, FreightNode]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen:
            return
        node = by_id[node_id]
        if node.parent is not None:
            visit(node.parent.node_id)
        seen.add(node_id)
        ordered.append(node_id)

    for doc in nodes:
        visit(doc["node_id"])
    return ordered

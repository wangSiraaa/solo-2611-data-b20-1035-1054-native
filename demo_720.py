#!/usr/bin/env python3
"""End-to-end demonstration of the seal-label / mold / freight rules (720).

The script always starts from a fresh database and walks through every
scenario in the task statement:

1. labels can only be pressed from really-consumed seal-clay portions;
2. the worked two-tooth-mold example and all of its variations;
3. the four-tooth mold with a two-cell voucher gap;
4. a false splice-mark claim that invalidates a position;
5. a store whose runs never legally started -> no release orders;
6. the cabinet-first workflow, a lost break-seal packet and reopen;
7. minimum-work-hours plans under the co-return contract.

Usage::

    python3 demo_720.py [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reagent.errors import Conflict  # noqa: E402
from reagent.inventory import InventoryService  # noqa: E402
from reagent.runs import RunService  # noqa: E402
from reagent.schema import connect, initialize  # noqa: E402
from seals import (  # noqa: E402
    ArchiveService,
    CostModel,
    PlanBlocked,
    SealLabelService,
    WorkflowService,
    build_tree,
    classify_leaves,
    initialize_seals,
    plan_dispositions,
)
from seals.matching import HOLE_MISSING, HOLE_PRESENT, Imprint, Mold, Voucher, reconstruct  # noqa: E402

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "720"

CABINET_TREE = [
    {"node_id": "CAB", "kind": "cabinet"},
    {"node_id": "X", "kind": "leaf", "parent_id": "CAB", "label_id": "LX"},
    {"node_id": "Y", "kind": "leaf", "parent_id": "CAB", "label_id": "LY"},
]
COSTS = CostModel(sealed_return={"CAB": 4}, unseal={"CAB": 1}, leaf_charge=2)


def heading(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def expect_conflict(label: str, fn) -> None:
    try:
        fn()
    except Conflict as exc:
        print(f"  - refused [{label}]: {exc}")
    else:
        raise AssertionError(f"expected a Conflict for {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    if args.db:
        db_path = args.db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass
    else:
        handle, db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)

    conn = connect(db_path)
    initialize(conn)
    initialize_seals(conn)
    inventory = InventoryService(conn)
    inventory.initialize(json.loads((MATERIALS / "inventory.json").read_text("utf-8")))
    runs = RunService(conn)
    seals = SealLabelService(conn)
    archive = ArchiveService(conn)
    workflow = WorkflowService(conn)

    # ------------------------------------------------------------------ (1)
    heading("1. 只有真正消耗的封签泥才能压具名箱签")
    runs.create_run("r-two", "seal", 2, 10, [{"bottle_id": "W1", "portions": 2}])
    runs.create_run("r-reserved", "seal", 1, 10, [{"bottle_id": "W1", "portions": 1}])
    runs.create_run("r-expired", "seal", 1, 10, [{"bottle_id": "W2", "portions": 1}])
    runs.start_run("r-two", 11)
    lx = seals.issue_label("LX", "X", "r-two", 11)
    ly = seals.issue_label("LY", "Y", "r-two", 11)
    print(f"  - 合法压签: {lx['label_id']} 用 {lx['bottle_id']} 第 {lx['portion_seq']} 份; "
          f"{ly['label_id']} 用第 {ly['portion_seq']} 份")
    expect_conflict("同一箱重复扫码", lambda: seals.issue_label("LX2", "X", "r-two", 11))
    expect_conflict("两份已用尽再压第三条",
                    lambda: seals.issue_label("LZ", "Z", "r-two", 11))
    expect_conflict("仅预留未开成",
                    lambda: seals.issue_label("LR", "R", "r-reserved", 11))
    expired = runs.start_run("r-expired", 12)
    print(f"  - 过期未开成的运行终态: {expired['state']}")
    expect_conflict("过期未开成",
                    lambda: seals.issue_label("LE", "E", "r-expired", 12))
    replay = runs.start_run("r-two", 13)
    print(f"  - 重复 start 回放原终态: replayed={replay['replayed']}，不另扣份数")

    # ------------------------------------------------------------------ (2)
    heading("2. 小例：坏炉 A 底模 00、好炉 B 底模 01，箱 X=11、箱 Y=10")
    archive.register_mold("mA", "A", "00", 13)
    archive.register_mold("mB", "B", "01", 13)
    archive.record_imprint("I-X", "LX", ["11"], 13)
    archive.record_imprint("I-Y", "LY", ["10"], 13)
    qx = archive.reconstruct_imprint("I-X")
    qy = archive.reconstruct_imprint("I-Y")
    print(f"  - 箱 X 可能炉批: {qx['possible_batches']} (模段 "
          f"{[(s['mold_id'], s['segments']) for s in qx['solutions']]})")
    print(f"  - 箱 Y 可能炉批: {qy['possible_batches']}")

    root, _ = build_tree(CABINET_TREE)
    facts = classify_leaves(root, archive, frozenset({"A"}))
    plan = plan_dispositions(root, facts, COSTS, max_co_returned_clear=1)
    print(f"  - 清楚印带、陪扣上限 1: 总工时 {plan['total_work_hours']} "
          f"-> {[(a['action'], a['node_id'], a['cost']) for a in plan['actions']]}")

    heading("2b. 两箱末格磨损读不出：任一箱都可能来自 A")
    archive.record_imprint("I-X", "LX", ["1?"], 14, note="末格磨损")
    archive.record_imprint("I-Y", "LY", ["1?"], 14, note="末格磨损")
    root, _ = build_tree(CABINET_TREE)
    facts = classify_leaves(root, archive, frozenset({"A"}))
    plan_worn = plan_dispositions(root, facts, COSTS, 1)
    print(f"  - 总工时 {plan_worn['total_work_hours']} "
          f"-> {[(a['action'], a['node_id']) for a in plan_worn['actions']]}")

    heading("2c. 印带仍清楚但外柜海关押封：只能原封扣回")
    # restore clear reads via a new version
    archive.record_imprint("I-X", "LX", ["11"], 15)
    archive.record_imprint("I-Y", "LY", ["10"], 15)
    locked_nodes = [dict(n, customs_locked=(n["node_id"] == "CAB")) for n in CABINET_TREE]
    root_l, _ = build_tree(locked_nodes)
    facts_l = classify_leaves(root_l, archive, frozenset({"A"}))
    plan_locked = plan_dispositions(root_l, facts_l, COSTS, 1)
    print(f"  - 总工时 {plan_locked['total_work_hours']} "
          f"-> {[(a['action'], a['node_id']) for a in plan_locked['actions']]}")

    heading("2d. 陪扣清白箱上限降为零（可开柜时仍为 3；押封柜则无解）")
    root_c, _ = build_tree(CABINET_TREE)
    facts_c = classify_leaves(root_c, archive, frozenset({"A"}))
    plan_zero = plan_dispositions(root_c, facts_c, COSTS, 0)
    print(f"  - 可开柜: 总工时 {plan_zero['total_work_hours']}，"
          f"陪扣清白箱 {plan_zero['co_returned_clear']}")
    try:
        plan_dispositions(root_l, facts_l, COSTS, 0)
    except Exception as exc:
        print(f"  - 押封柜上限 0: {type(exc).__name__}: {exc}")

    heading("2e. X、Y 都清楚印 11：可同时出自 A 的同一模段（不冲突）")
    archive.record_imprint("I-X", "LX", ["11"], 16)
    archive.record_imprint("I-Y", "LY", ["11"], 16)
    shared_x = archive.reconstruct_imprint("I-X")["solutions"]
    shared_y = archive.reconstruct_imprint("I-Y")["solutions"]
    same = (
        shared_x[0]["mold_id"] == shared_y[0]["mold_id"]
        and shared_x[0]["segments"] == shared_y[0]["segments"]
    )
    print(f"  - 同一模段被两只箱复用: {same} "
          f"(模段 {shared_x[0]['mold_id']} {shared_x[0]['segments']})")

    # ------------------------------------------------------------------ (3)
    heading("3. 四齿底模 0110 与两小段印纹 1、1：凭据跨过两格")
    mold4 = Mold("m4", "C", "0110")
    voucher = Voucher("v-2", "m4", "shift-16", 1, 2, 2, 2)
    imprint = Imprint(("1", "1"), frozenset(), HOLE_PRESENT)
    with_voucher, _ = reconstruct(imprint, (mold4,), (voucher,))
    print("  - 有废边凭据:",
          [(s.direction, [(p.mold_start, p.mold_end) for p in s.placements],
            [(g.skip, g.voucher_id) for g in s.gaps]) for s in with_voucher])
    without, fail = reconstruct(imprint, (mold4,), ())
    print(f"  - 去掉凭据: 解数 {len(without)}；证据: {fail[0].reasons}")
    # persist the same facts through the archive service
    archive.register_mold("m4", "C", "0110", 16)
    archive.register_voucher("v-2", "m4", "shift-16", 1, 2, 2, 2)

    # ------------------------------------------------------------------ (4)
    heading("4. 在无痕连续段上声称接痕：定位失效的证据")
    markless = Mold("m-markless", "D", "000")
    claimed = Imprint(("11",), frozenset({0}), HOLE_PRESENT)
    solutions, failures = reconstruct(claimed, (markless,), ())
    print(f"  - 解数 {len(solutions)}；让定位失效的证据:")
    for reason in failures[0].reasons:
        print(f"      * {reason}")

    # ------------------------------------------------------------------ (5)
    heading("5. 旧运行全部开领失败：无合法箱签，停单不签放行")
    # a fresh, independent store built from an inventory whose only seal run
    # expired before it could start: no consumed portion, no legal label
    handle, failed_db = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    failed_conn = connect(failed_db)
    initialize(failed_conn)
    initialize_seals(failed_conn)
    failed_inventory = InventoryService(failed_conn)
    failed_inventory.initialize(
        json.loads((MATERIALS / "inventory.json").read_text("utf-8"))
    )
    failed_runs = RunService(failed_conn)
    failed_seals = SealLabelService(failed_conn)
    failed_archive = ArchiveService(failed_conn)
    failed_runs.create_run(
        "dead-run", "seal", 1, 10, [{"bottle_id": "W2", "portions": 1}]
    )
    dead = failed_runs.start_run("dead-run", 12)  # W2 expires on day 12
    print(f"  - 旧运行终态: {dead['state']}，原因: {dead['reason']}")
    try:
        failed_seals.issue_label("DEAD-LABEL", "DEAD-BOX", "dead-run", 12)
    except Conflict as exc:
        print(f"  - 压签被拒: {exc}")
    print(f"  - 合法箱签数量: {len(failed_seals.list_labels())}")
    # register a mold; with no legal label no imprint can be archived, so no
    # origin is ever reconstructable and no release order can be signed
    failed_archive.register_mold("mA", "A", "00", 13)
    try:
        failed_archive.record_imprint("I-DEAD", "DEAD-LABEL", ["11"], 13)
    except Exception as exc:
        print(f"  - 无合法签即无法建档印带: {type(exc).__name__}: {exc}")
    failed_conn.close()
    os.unlink(failed_db)

    # the same stop-order guard fires when a leaf references a label id that
    # was never legally issued in the main store
    ghost_tree = CABINET_TREE + [
        {"node_id": "G", "kind": "leaf", "parent_id": "CAB", "label_id": "GHOST"}
    ]
    root_ghost, _ = build_tree(ghost_tree)
    try:
        classify_leaves(root_ghost, archive, frozenset({"A"}))
    except PlanBlocked as blocked:
        for item in blocked.blockers:
            print(f"  - 停单: 节点 {item['node_id']} ({item['label_id']}): {item['reason']}")

    # ------------------------------------------------------------------ (6)
    heading("6. 柜先交割：破封回包丢失后重开，同一只箱不会既发又扣")
    # rebuild a clean two-leaf case on a fresh tree id
    workflow.register_tree(CABINET_TREE)
    archive.record_imprint("I-X", "LX", ["11"], 17)
    archive.record_imprint("I-Y", "LY", ["10"], 17)
    root, _ = build_tree(CABINET_TREE)
    facts = classify_leaves(root, archive, frozenset({"A"}))
    plan = plan_dispositions(root, facts, COSTS, 1)
    workflow.open_workflow("wf-720", 18)
    workflow.approve_plan("wf-720", plan)
    workflow.lock_cabinet("wf-720", "CAB", 18)
    receipt = workflow.break_seal("wf-720", "CAB", 18,
                                  {"seal_serial": "S-720", "officer": "liu"})
    print(f"  - 破封回执已落库: {receipt['break_receipt']}")
    workflow.close_workflow("wf-720", 18)
    recovery = workflow.reopen_workflow("wf-720", 19)
    print(f"  - 重开认出已破封: {[s['node_id'] for s in recovery['broken_seals']]}")
    print(f"  - 待办动作: {[(a['action'], a['node_id']) for a in recovery['pending_actions']]}")
    replay = workflow.break_seal("wf-720", "CAB", 19, {"seal_serial": "S-720"})
    print(f"  - 再次破封只回放: replayed={replay['replayed']}，状态仍为 {replay['state']}")
    for action in recovery["pending_actions"]:
        workflow.execute_action("wf-720", action, 19)
    duplicate = workflow.execute_action(
        "wf-720", {"action": "release", "node_id": "Y"}, 19
    )
    print(f"  - 重复放行 Y 被幂等拦住: {duplicate}")
    final = workflow.recovery_state("wf-720")
    print("  - 最终每箱唯一去向:")
    for item in final["dispositions"]:
        print(f"      {item['label_id']}: {item['action']} via {item['via']}")
    workflow.finalize_workflow("wf-720", 20)
    print(f"  - 工作流终态: {workflow.get_workflow('wf-720')['state']}")

    conn.close()
    if not args.db:
        os.unlink(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

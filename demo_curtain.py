#!/usr/bin/env python3
"""Curtain tour demo: library checkout -> reconciliation -> sewing plan -> truck.

Runs the three local steps plus the sealed-truck tour on top of the reagent
store, and prints a hand-checkable trace:

1. 原库领用   two runs each consume one portion of the reference bottle REF
2. 测色复核   countersign the records; station B's offset is pinned to 3
3. 排幕       tolerance 1 keeps both old panels; tolerance 0 swaps one
4. 失效变体   the day-14 checkout fails (opened expiry), B's reading is
              untrusted, the old seam ranges [1, 4] and one panel is swapped
5. 重新核准   revoke / re-approve the B reference; the old loading list stays
              as sealed while a new list is signed from the new revision
6. 巡演车     one sealed loading list, a spare lost after loading, two
              troupes competing for the same physical panel
7. 原库存查询 the original reagent queries still work on the same database

Usage::

    python3 demo_curtain.py [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reagent.cli import execute_operations  # noqa: E402
from reagent.errors import Conflict, ReagentError  # noqa: E402
from reagent.inventory import InventoryService  # noqa: E402
from reagent.schema import connect, initialize  # noqa: E402

from curtain.errors import NoArrangement  # noqa: E402
from curtain.schema import initialize_curtain  # noqa: E402
from curtain.service import CurtainService  # noqa: E402

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "curtain"


def _load(name: str) -> dict:
    return json.loads((MATERIALS / name).read_text("utf-8"))


def _fresh_store(db_path: str) -> tuple:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass
    conn = connect(db_path)
    initialize(conn)
    initialize_curtain(conn)
    inventory = InventoryService(conn)
    inventory.initialize(_load("inventory.json"))
    return conn, inventory


def _placement_line(plan: dict) -> str:
    parts = []
    for placement in plan["placements"]:
        if placement["choice"] == "old":
            parts.append(f"pos{placement['position']}=旧幅{placement['panel_id']}")
        else:
            flip = "（对调）" if placement["flipped"] else ""
            parts.append(
                f"pos{placement['position']}=备布{placement['panel_id']}{flip}"
            )
    return f"换 {plan['swaps']} 幅: " + ", ".join(parts)


def _run_checkouts(inventory: InventoryService, conn, operations: str) -> None:
    results = execute_operations(conn, _load(operations))
    for entry in results:
        result = entry["result"]
        if entry["op"] == "create":
            print(f"  create {entry['run_id']:10} -> {result['state']}")
        elif entry["op"] == "start":
            day = result.get("started_day") or result.get("released_day")
            print(f"  start  {entry['run_id']:10} -> {result['state']:8} day={day}")
            if result.get("reason"):
                print(f"         原因: {result['reason']}")
    alloc = inventory.query("REF", 13)
    print(
        f"  REF: consumed={alloc['consumed']} unallocated={alloc['unallocated']} "
        f"opened_day={alloc['opened_day']} "
        f"effective_expiry={alloc['effective_expiry_day']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    if args.db:
        db_path = args.db
    else:
        handle, db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)

    conn, inventory = _fresh_store(db_path)
    curtain = CurtainService(conn)

    print("== 1. 原库领用（真实时间线：两次运行各领出一份）==")
    _run_checkouts(inventory, conn, "operations_real.json")

    print("\n== 2. 测色复核（核签记录，联合解释）==")
    print("  setup:", curtain.setup(_load("curtain.json"), 13))
    curtain.register_reference("ref-A", "REF", "A", 10, 0, 13)
    curtain.register_reference("ref-B", "REF", "B", 13, 0, 13)
    rev1 = curtain.countersign(13)
    print(f"  核签 {rev1['revision_id']}: feasible={rev1['feasible']}")
    print(f"  见证: 台偏差 {rev1['witness']['stations']} "
          f"瓶真色 {rev1['witness']['bottles']}")
    seam = curtain.seam_query(rev1["revision_id"], "eO2L", "eO1R")
    print(f"  旧缝 eO2L-eO1R 可达色差: [{seam['min']}, {seam['max']}]")
    probe = curtain.seam_query(rev1["revision_id"], "eD2", "eD1")
    print(
        f"  同台 B 两条布边 eD2-eD1: 联合解释 [{probe['min']}, {probe['max']}]"
        f"（各自区间 [11,14]-[7,10] 再相减会错算成 [1,7]）"
    )

    print("\n== 3. 排幕（例一：容差 1 可直接挂；例三：容差收紧到 0 换一幅）==")
    plan_keep = curtain.plan("ex", rev1["revision_id"])
    print(f"  容差 1: {_placement_line(plan_keep)}")
    plan_tight = curtain.plan("ex0", rev1["revision_id"])
    print(f"  容差 0: {_placement_line(plan_tight)}")

    print("\n== 4. 失效变体（乙的领用开封到期，参照不可采信）==")
    variant_path = db_path + ".variant"
    vconn, vinventory = _fresh_store(variant_path)
    vcurtain = CurtainService(vconn)
    _run_checkouts(vinventory, vconn, "operations_expired.json")
    vcurtain.setup(_load("curtain.json"), 14)
    vcurtain.register_reference("ref-A", "REF", "A", 10, 0, 14)
    try:
        vcurtain.register_reference("ref-B", "REF", "B", 13, 0, 14)
    except Conflict as exc:
        print(f"  ref-B 登记被拒: {exc}")
    vrev = vcurtain.countersign(14)
    vseam = vcurtain.seam_query(vrev["revision_id"], "eO2L", "eO1R")
    print(
        f"  核签 {vrev['revision_id']}: 台乙偏差见证取 "
        f"{vrev['witness']['stations']['B']}（只知在 [0,3]），"
        f"旧缝色差可达 [{vseam['min']}, {vseam['max']}]"
    )
    vplan = vcurtain.plan("ex", vrev["revision_id"])
    print(f"  容差 1: {_placement_line(vplan)}（应换一幅）")
    vconn.close()

    print("\n== 5. 重新核准（旧装车单原样留存）==")
    curtain.revoke_reference("ref-B", 14)
    rev2 = curtain.countersign(14)
    seam2 = curtain.seam_query(rev2["revision_id"], "eO2L", "eO1R")
    print(
        f"  撤回 ref-B 后核签 {rev2['revision_id']}: "
        f"旧缝可达 [{seam2['min']}, {seam2['max']}]"
    )
    list_old = curtain.seal_loading_list(
        "ex-list-old", rev2["revision_id"], ["ex"], ["S0"], 14
    )
    print(
        f"  旧装车单 ex-list-old: 修订 {list_old['revision_id']}，"
        f"{_placement_line(list_old['shows']['ex']['primary'])}"
    )
    curtain.reapprove_reference("ref-B", 15)
    rev3 = curtain.countersign(15)
    print(
        f"  重新核准 ref-B 后核签 {rev3['revision_id']}"
        f"（回到 ref-B 撤回前的记录集: {rev3['revision_id'] == rev1['revision_id']}）"
    )
    list_new = curtain.seal_loading_list(
        "ex-list-new", rev3["revision_id"], ["ex"], ["S0"], 15
    )
    print(
        f"  新装车单 ex-list-new: 修订 {list_new['revision_id']}，"
        f"{_placement_line(list_new['shows']['ex']['primary'])}"
    )
    kept = curtain.get_list("ex-list-old")
    print(
        f"  旧单原样留存: 修订仍是 {kept['revision_id']}，"
        f"仍换 {kept['shows']['ex']['primary']['swaps']} 幅"
    )
    try:
        curtain.seal_loading_list(
            "ex-list-empty", rev2["revision_id"], ["ex"], [], 15
        )
    except NoArrangement as exc:
        print(f"  空车签单被拒（见证须与装幅相符）: {exc}")

    print("\n== 6. 巡演车（一张实物装车单，两站交割）==")
    tour = curtain.seal_loading_list(
        "tour-list", rev1["revision_id"], ["T1", "T2"],
        ["S1", "S2", "S3", "S4"], 16,
    )
    for show_id, doc in sorted(tour["shows"].items()):
        print(f"  {show_id} 主排法: {_placement_line(doc['primary'])}")
        missing_s1 = doc["contingencies"].get("S1")
        if missing_s1 is not None and missing_s1 != doc["primary"]:
            print(f"  {show_id} 少 S1 时: {_placement_line(missing_s1)}")
    curtain.depart("tour-list", 16)
    print("  车已出库；装车后发现 S2 缺失，车上实有 S1,S3,S4")
    onsite = curtain.onsite("tour-list", ["S1", "S3", "S4"])
    for show_id, answer in sorted(onsite.items()):
        print(f"  驻场核实 {show_id}: {answer['options']}")
    print("  （凭车上封好的清单核实，不用回库）")
    what_if = curtain.onsite("tour-list", ["S3", "S4"])
    print(f"  若 S1 也缺失: T1 can_deliver={what_if['T1']['can_deliver']}（无法交幕）")
    handover1 = curtain.handover("tour-list", "T1", "primary", ["S1", "S3", "S4"], 17)
    print(f"  T1 交割: {handover1['arrangement_key']} 锁定 {handover1['locked']}")
    try:
        curtain.handover("tour-list", "T2", "primary", ["S1", "S3", "S4"], 18)
    except Conflict as exc:
        print(f"  T2 主排法争 S1 被拒: {exc}")
    onsite2 = curtain.onsite("tour-list", ["S3", "S4"])
    print(f"  T2 驻场核实（实有 S3,S4）: {onsite2['T2']['options']}")
    handover2 = curtain.handover(
        "tour-list", "T2", "missing:S1", ["S3", "S4"], 18
    )
    print(f"  T2 交割: {handover2['arrangement_key']} 锁定 {handover2['locked']}")
    print(f"  锁定台账: {[(l['panel_id'], l['show_id']) for l in curtain.locks()]}")

    print("\n== 7. 原库存查询（同一数据库，查询保留）==")
    for day in (13, 14):
        alloc = inventory.query("REF", day)
        print(
            f"  REF day {day}: unallocated={alloc['unallocated']} "
            f"leadable_today={alloc['leadable_today']} "
            f"consumed={alloc['consumed']} opened_day={alloc['opened_day']}"
        )
    for row in inventory.conservation():
        lhs = row["total_portions"]
        rhs = row["unallocated"] + row["reserved"] + row["consumed"]
        print(
            f"  守恒 {row['bottle_id']}: {lhs} == {row['unallocated']}+"
            f"{row['reserved']}+{row['consumed']} -> {'OK' if lhs == rhs else 'FAIL'}"
        )

    conn.close()
    if not args.db:
        os.unlink(db_path)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + ".variant" + suffix)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Weaving-room demonstration over the reagent store.

Scenarios from the task, each on a fresh database::

  A. 机头在左, R(左,2) / B(右,1), 纹样红蓝红, 票 1  -> 恰用一票
  B. 蓝芯运行因有效期失败 -> 蓝梭装不出线, 织单被拒; 票额减为 0 同样拒单
  C. 再加一只左侧红梭一份 -> 零票可织
  D. 排练后该梭被另一台机交接走 -> 正式开织重新核实后改判失败
  E. 中途关机 -> 从最后实际完成行接织, 不重复耗线; 回执重发幂等
  F. 整带作废 -> 按未织后缀重编新带继续

Also demonstrated: unload of an empty shuttle, cross-loom handoff carrying
remaining thread, and the shuttle provenance trace with no weave job at hand.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loom import LoomService  # noqa: E402
from loom.errors import LoomConflict  # noqa: E402
from reagent import connect, initialize  # noqa: E402
from reagent.inventory import InventoryService  # noqa: E402

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "loom"


def fresh_store() -> tuple:
    handle, db_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    conn = connect(db_path)
    initialize(conn)
    doc = json.loads((MATERIALS / "inventory.json").read_text("utf-8"))
    InventoryService(conn).initialize(doc)
    return conn, LoomService(conn), db_path


def title(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def load_shuttle(svc: LoomService, load_id, shuttle, loom, bottle, portions,
                 reserve_day=10, confirm_day=10):
    svc.reserve_load(load_id, shuttle, loom, bottle, portions, reserve_day)
    return svc.confirm_load(load_id, confirm_day)


# ---------------------------------------------------------------------------
def scenario_a() -> LoomService:
    title("A. 红蓝红, 机头在左, R(左,2) B(右,1), 票额 1")
    conn, svc, _ = fresh_store()
    svc.register_loom("L1", "left", 10)
    svc.register_shuttle("R", "core-R", "R", "left", 10)
    svc.register_shuttle("B", "core-B", "B", "right", 10)
    load_shuttle(svc, "load-R", "R", "L1", "R-bottle", 2)
    load_shuttle(svc, "load-B", "B", "L1", "B-bottle", 1)
    print("装梭后:", svc.get_shuttle("R"), "|", svc.get_shuttle("B"))

    job = svc.create_weave_job("job-A", "L1", ["R", "B", "R"], 1, 10)
    print("执行带:")
    for step in job["steps"]:
        print(f"  {step['step_index']}: {step['kind']:5} "
              f"{step['shuttle_id']} @{step['side']} {step.get('color') or ''}")
    result = svc.weave_all("job-A", 10)
    print("成品:", json.dumps(result["product"], ensure_ascii=False))
    print("最终机头:", svc.get_loom("L1")["head_side"],
          " R 余线/边:", svc.get_shuttle("R")["remaining"], svc.get_shuttle("R")["side"],
          " B 余线/边:", svc.get_shuttle("B")["remaining"], svc.get_shuttle("B")["side"])
    return svc


def scenario_b() -> None:
    title("B. 只让蓝芯旧运行因有效期失败 -> 蓝梭装不出线, 不能接单")
    conn, svc, _ = fresh_store()
    svc.register_loom("L1", "left", 10)
    svc.register_shuttle("R", "core-R", "R", "left", 10)
    svc.register_shuttle("B", "core-B", "B", "right", 10)
    load_shuttle(svc, "load-R", "R", "L1", "R-bottle", 2)

    # reserve on day 10; the B bottle's printed expiry is day 11, so starting
    # on day 11 hits the boundary day and fails validity (old library rule).
    svc.reserve_load("load-B", "B", "L1", "B-bottle", 1, 10)
    receipt = svc.confirm_load("load-B", 11)
    print("蓝运行实领回执 state =", receipt["state"], "| reason =", receipt["reason"])
    print("蓝梭仍未上机:", svc.get_shuttle("B"))
    # Replaying the same confirm does not create a second attempt.
    replay = svc.confirm_load("load-B", 11)
    print("重发确认回执 replayed =", replay["replayed"], "state =", replay["state"])

    for budget in (1, 0):
        try:
            svc.create_weave_job(f"job-B{budget}", "L1", ["R", "B", "R"], budget, 11)
        except LoomConflict as exc:
            print(f"票额 {budget} 接单被拒: {exc}")

    # The successful red run is still real stock; conservation is intact.
    print("R 瓶守恒:", InventoryService(conn).query("R-bottle", 11))


def scenario_c() -> None:
    title("C. 再给一只左侧红梭一份 -> 零票能织全红单")
    conn, svc, _ = fresh_store()
    svc.register_loom("L1", "left", 10)
    svc.register_shuttle("R1", "core-R1", "R", "left", 10)
    svc.register_shuttle("R2", "core-R2", "R", "left", 10)
    svc.register_shuttle("B", "core-B", "B", "right", 10)
    load_shuttle(svc, "load-R1", "R1", "L1", "R-bottle", 2)
    load_shuttle(svc, "load-R2", "R2", "L1", "R-spare", 1)
    load_shuttle(svc, "load-B", "B", "L1", "B-bottle", 1)
    job = svc.create_weave_job("job-C", "L1", ["R", "R", "R"], 0, 10)
    print("零票执行带:", [(s["kind"], s["shuttle_id"]) for s in job["steps"]])
    result = svc.weave_all("job-C", 10)
    print("成品用票:", result["product"]["tickets_used"],
          "余量去向:",
          [(d["shuttle_id"], d["remaining"]) for d in result["product"]["shuttle_destinations"]])


def scenario_d() -> None:
    title("D. 排练后 R2 被另一台机交接走 -> 开织重新核实改判")
    conn, svc, _ = fresh_store()
    svc.register_loom("L1", "left", 10)
    svc.register_loom("L2", "left", 10)
    svc.register_shuttle("R1", "core-R1", "R", "left", 10)
    svc.register_shuttle("R2", "core-R2", "R", "left", 10)
    load_shuttle(svc, "load-R1", "R1", "L1", "R-bottle", 2)
    load_shuttle(svc, "load-R2", "R2", "L1", "R-spare", 1)
    job = svc.create_weave_job("job-D", "L1", ["R", "R", "R"], 0, 10)
    print("已编排执行带:", [(s["kind"], s["shuttle_id"]) for s in job["steps"]])

    # Rehearsal window: another machine takes R2 away (whole shuttle, thread
    # included). The handoff receipt is idempotent.
    handoff = svc.handoff_shuttle("handoff-R2", "R2", "L2", 10)
    again = svc.handoff_shuttle("handoff-R2", "R2", "L2", 10)
    print("交接回执:", handoff["event_type"], "replayed =", again["replayed"])
    print("R2 现在所在机/侧/余线:", svc.get_shuttle("R2")["loom_id"],
          svc.get_shuttle("R2")["side"], svc.get_shuttle("R2")["remaining"])

    check = svc.verify_job("job-D")
    print("正式开织核实:", check)
    outcome = svc.weave_next("job-D", 10)
    print("强行驱动织单结果:", outcome["state"], "-", outcome.get("reason"))


def scenario_e() -> None:
    title("E. 中途关机: 只完成前两行, 重启后续织且不重复耗线")
    conn, svc, _ = fresh_store()
    svc.register_loom("L1", "left", 10)
    svc.register_shuttle("R", "core-R", "R", "left", 10)
    svc.register_shuttle("B", "core-B", "B", "right", 10)
    load_shuttle(svc, "load-R", "R", "L1", "R-bottle", 2)
    load_shuttle(svc, "load-B", "B", "L1", "B-bottle", 1)
    svc.create_weave_job("job-E", "L1", ["R", "B", "R"], 1, 10)
    svc.weave_next("job-E", 10)
    svc.weave_next("job-E", 10)  # power cut after the second physical row
    before = svc.get_job("job-E")
    print(f"关机点: rows_done={before['rows_done']} "
          f"tickets_used={before['tickets_used']} state={before['state']}")
    print("  R 余线/边:", svc.get_shuttle("R")["remaining"], svc.get_shuttle("R")["side"],
          " B 余线/边:", svc.get_shuttle("B")["remaining"], svc.get_shuttle("B")["side"])

    # Re-issuing the last physical receipt never consumes another portion.
    last_event = before["steps"][1]["planned_event_id"]
    replay = svc.pass_shuttle(last_event, "B", 10)
    print("重发过梭回执 replayed =", replay["replayed"],
          "B 余线仍为 =", svc.get_shuttle("B")["remaining"])

    result = svc.weave_all("job-E", 10)  # resume from row 3
    print("续织成品:", json.dumps(result["product"], ensure_ascii=False))

    # Provenance trace without needing the job.
    trace = svc.shuttle_trace("R")
    print("R 的交接点查询: 余线 =", trace["remaining"],
          "机 =", trace["current_loom"], "侧 =", trace["current_side"])
    print("  使它如此的回执链:",
          [(e["event_id"], e["event_type"], e["portions_delta"]) for e in trace["events"]])


def scenario_f() -> None:
    title("F. 整带作废后按未织后缀重编, 并演示卸空梭/跨机余线同行")
    conn, svc, _ = fresh_store()
    svc.register_loom("L1", "left", 10)
    svc.register_loom("L2", "left", 10)
    svc.register_shuttle("R", "core-R", "R", "left", 10)
    svc.register_shuttle("B", "core-B", "B", "right", 10)
    load_shuttle(svc, "load-R", "R", "L1", "R-bottle", 3)
    load_shuttle(svc, "load-B", "B", "L1", "B-bottle", 2)

    svc.create_weave_job("job-F1", "L1", ["R", "B", "R", "B", "R"], 3, 10)
    for _ in range(3):  # two physical rows then one carry; power cut at step 3
        svc.weave_next("job-F1", 10)
    partial = svc.get_job("job-F1")
    print("作废前 rows_done =", partial["rows_done"],
          "机头 =", svc.get_loom("L1")["head_side"])
    svc.void_job("job-F1", "paper instruction superseded", 10)
    print("旧带状态 =", svc.get_job("job-F1")["state"],
          "已完成行保留 =", svc.get_job("job-F1")["rows_done"])

    # Replan only the un-woven suffix from the current physical positions.
    suffix = svc.create_weave_job("job-F2", "L1", ["B", "R"], 3, 10)
    print("后缀执行带:",
          [(s["kind"], s["shuttle_id"], s.get("color")) for s in suffix["steps"]])
    product = svc.weave_all("job-F2", 10)["product"]
    print("后缀成品行数 =", product["rows_woven"], "用票 =", product["tickets_used"])

    # B is now empty: it can be unloaded; its portions never return to bottles.
    print("B 余线 =", svc.get_shuttle("B")["remaining"])
    unload = svc.unload_shuttle("unload-B", "B", 10)
    print("卸梭回执:", unload["event_type"], "->", svc.get_shuttle("B")["state"])

    # R still has thread; hand the whole shuttle to L2, thread travels with it.
    remaining_before = svc.get_shuttle("R")["remaining"]
    svc.handoff_shuttle("handoff-R", "R", "L2", 10)
    r = svc.get_shuttle("R")
    print(f"整梭交接: R 余线 {remaining_before} 随行, 现于 {r['loom_id']} "
          f"{r['side']} 侧, 余线 {r['remaining']}")
    print("R 瓶未用量不回退:", InventoryService(conn).query("R-bottle", 10)["unallocated"])


def main() -> int:
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    print("\n全部场景演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

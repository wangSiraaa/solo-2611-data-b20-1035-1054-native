#!/usr/bin/env python3
"""Offline end-to-end demonstration.

Three independently re-openable local counters (JIA=总馆/天平室, YI, BING),
thirteen replica coins across three mint periods, and the four scenarios from
the brief:

1. happy path  -- one seal split into two, reweighed at two branches,
   returned, written off with one shared mass interpretation;
2. contradictory weighing receipt -- an extra plate that would flip an
   established appraisal is refused by the weighing room;
3. double spend -- a branch that secretly re-splits a seal cannot also write
   the old seal off;
4. globally incompatible reweighs -- each receipt alone is plausible but no
   single mass table supports the whole trip;
plus a crash/restart interlude proving pending work survives a counter
reopening and retransmitted requests never move a seal twice.

Run:  python3 museum_demo.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from cs.store import Store
from museum.counter import Counter
from museum.scale import ScaleOffice
from museum.wire import MessageBus

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "tour"
WORK = ROOT / "demo" / "tour"

DOMAINS = {"A": [1, 3], "B": [1, 3], "C": [2, 5]}
NODES = ["JIA", "YI", "BING"]


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, value: object) -> None:
    print(f"-- {label}")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def fresh_workspace() -> tuple[MessageBus, Counter, Counter, Counter, Store, ScaleOffice]:
    if WORK.exists():
        shutil.rmtree(WORK)
    spool = WORK / "spool"
    bus = MessageBus(spool)
    store = Store.open(str(WORK / "ticketlib.db"))
    catalog = json.loads((MATERIALS / "catalog.json").read_text(encoding="utf-8"))
    store.init_catalog(catalog)
    tickets = [
        json.loads(line)
        for line in (MATERIALS / "tickets.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    store.apply_fact_revisions(tickets, request_id="seed-tickets")
    store.withdraw("W-GONE", 1, request_id="withdraw-gone")
    office = ScaleOffice.open(str(WORK / "scale.db"), store)
    office.declare_domains(DOMAINS)

    hq = Counter("JIA", WORK / "jia.db", bus, role="hq")
    yi = Counter("YI", WORK / "yi.db", bus, role="branch")
    bing = Counter("BING", WORK / "bing.db", bus, role="branch")
    return bus, hq, yi, bing, store, office


def pump(*counters: Counter) -> None:
    """Deliver every queued message until the mailboxes are quiet."""

    for _ in range(10):
        moved = False
        for counter in counters:
            if counter.run_inbox():
                moved = True
            if counter.run_acks():
                moved = True
        if not moved:
            break


# --------------------------------------------------------------------- scenes
def scene_ticket_library(store: Store, office: ScaleOffice) -> None:
    banner("场景 0：工单库分类、多路径去重、零/多铸期拒签、撤回行不装盘")
    # A1/A2 both reach A; B1 reaches B; X reaches both A and B; C reaches C.
    result = store.query(["A", "B", "C"])
    summary = {
        g["target_id"]: {
            "ticket_count": g["count"],
            "coin_amount": g["amount"],
            "tickets": [c["ticket_id"] for c in g["contributions"]],
        }
        for g in result["groups"]
    }
    show("同一 cut 下各铸期归集（多条路径只算一次）", summary)

    plate_ok = office.sign_plate("PL-甲期", ["W-A1", "W-A2"], (5, 15))
    print(f"PL-甲期: state={plate_ok.state} counts={plate_ok.counts}")
    plate_dup = office.sign_plate("PL-跨期", ["W-DUP"], (1, 3))
    print(f"PL-跨期(零或多铸期行): state={plate_dup.state} error={plate_dup.errors[:1]}")
    plate_gone = office.sign_plate("PL-撤回", ["W-GONE"], (1, 5))
    print(
        f"PL-撤回: state={plate_gone.state} errors={plate_gone.errors} "
        f"warnings={plate_gone.warnings}"
    )


def scene_weighing_conflict(office: ScaleOffice) -> None:
    banner("场景 1：天平室联合鉴定、矛盾回单拒绝、配重问答")
    # The original appraisal: one A and one B on the same plate, net weight 4.
    original = office.sign_plate("PL-原鉴定", ["W-ONE-A", "W-ONE-B"], (4, 4))
    print(f"PL-原鉴定: state={original.state} counts={original.counts}")
    witness = office.compatibility()
    show("全部在册回单的一套实在配重", witness)

    # An extra plate claiming the single A coin weighs 3.  It is physically
    # possible in isolation, but accepting it would flip the established
    # appraisal (A=1, B=3), so the weighing room refuses.
    extra = office.sign_plate("PL-矛盾A3", ["W-ONE-A"], (3, 3))
    print(f"PL-矛盾A3: state={extra.state}")
    show("拒绝凭据", extra.errors)
    show(
        "在册有效回单（矛盾盘未污染鉴定）",
        [r["receipt_id"] for r in office.list_receipts()],
    )
    joint = office.joint_appraisal(["PL-甲期", "PL-原鉴定"])
    show("多盘联合鉴定", {"verdict": joint["verdict"], "masses": joint["masses"]})
    fixed = office.check_mass_table({"A": 1, "B": 3, "C": 2})
    show("持原鉴定核验指定配重", fixed)


def scene_happy_tour(hq: Counter, yi: Counter, bing: Counter) -> dict:
    banner("场景 2：十三枚币原封出库 → 转寄 → 拆并 → 两馆复称 → 整封归还 → 核销")
    # Original seals on the outbound ledger (13 coins total).
    hq.hq_dispatch("B-DISPATCH-1", "S-甲乙", "YI", {"A": 5, "B": 4})
    hq.hq_dispatch("B-DISPATCH-2", "S-丙", "BING", {"C": 2, "A": 2})
    pump(hq, yi, bing)
    print("出库后 乙持有:", [s["seal_id"] for s in yi.held_seals()])
    print("出库后 丙持有:", [s["seal_id"] for s in bing.held_seals()])

    # YI forwards S-甲乙 whole to BING (no opening needed), then gets it back
    # via a second whole-seal forward to exercise the no-open path.
    yi.request_forward("B-FWD-1", "S-甲乙", "BING")
    pump(hq, yi, bing)
    bing.request_forward("B-FWD-2", "S-甲乙", "YI")
    pump(hq, yi, bing)
    print("两次整封转寄后 乙持有:", [s["seal_id"] for s in yi.held_seals()])

    # YI opens S-甲乙 and splits it: a1 (A coins) stays, b1 (B coins) goes BING.
    yi.request_split_merge(
        "B-SPLIT-1",
        ["S-甲乙"],
        [
            {"seal_id": "a1", "holder": "YI", "contents": {"A": 5}},
            {"seal_id": "b1", "holder": "BING", "contents": {"B": 4}},
        ],
    )
    pump(hq, yi, bing)
    print("拆并后 乙持有:", [s["seal_id"] for s in yi.held_seals()],
          "丙持有:", [s["seal_id"] for s in bing.held_seals()])

    # BING merges its two B/A/C holdings into one cross-period seal plus a C seal.
    bing.request_split_merge(
        "B-MERGE-1",
        ["S-丙", "b1"],
        [
            {"seal_id": "m1", "holder": "BING", "contents": {"A": 2, "B": 4}},
            {"seal_id": "c1", "holder": "BING", "contents": {"C": 2}},
        ],
    )
    pump(hq, yi, bing)
    print("合并后 丙持有:", [s["seal_id"] for s in bing.held_seals()])

    # Branch reweighs (intervals must fit the one shared interpretation A=1,B=3,C=2).
    yi.request_reweigh("a1", (5, 5))       # 5 A coins * 1 = 5
    pump(hq, yi, bing)
    bing.request_reweigh("m1", (14, 14))   # 2*1 + 4*3 = 14
    pump(hq, yi, bing)
    bing.request_reweigh("c1", (4, 4))     # 2 C coins * 2 = 4
    pump(hq, yi, bing)

    # Both branches return their terminal seals.
    yi.request_return("a1")
    bing.request_return("m1")
    bing.request_return("c1")
    pump(hq, yi, bing)

    receipts = [
        {"receipt_id": "PL-甲期", "counts": {"A": 5}, "lo": 5, "hi": 15},
        {"receipt_id": "PL-原鉴定", "counts": {"A": 1, "B": 1}, "lo": 4, "hi": 4},
    ]
    report = hq.settle(DOMAINS, receipts)
    show(
        "总馆核销报告（逐件去向 + 全程同一配重）",
        {
            "ok": report["ok"],
            "problems": report["problems"],
            "in_transit_seals": report["in_transit_seals"],
            "returned_seals": report["returned_seals"],
            "masses": report["weight_report"]["masses"],
            "sample_coin_trace": {
                cid: report["coin_traces"][cid]
                for cid in list(report["coin_traces"])[:2]
            },
        },
    )
    return report


def scene_restart(bus: MessageBus, hq: Counter, yi: Counter) -> None:
    banner("场景 3：柜台重开、断线待结、重传不重复转移")
    # YI starts a new handover while its connection to HQ is "offline": the
    # request is persisted locally and nothing reaches HQ yet.
    hq.hq_dispatch("B-DISPATCH-3", "S-断线", "YI", {"A": 1, "C": 1})
    pump(hq, yi)
    yi.request_split_merge(
        "B-SPLIT-OFFLINE",
        ["S-断线"],
        [
            {"seal_id": "d1", "holder": "YI", "contents": {"A": 1}},
            {"seal_id": "d2", "holder": "BING", "contents": {"C": 1}},
        ],
    )
    pending_before = len(yi.pending_requests())
    print(f"断线下乙待结请求: {pending_before}")

    # Reopen every counter from its SQLite files; outbox survives.
    hq.close()
    yi.close()
    hq2 = Counter.reopen("JIA", WORK / "jia.db", bus)
    yi2 = Counter.reopen("YI", WORK / "yi.db", bus)
    bing2 = Counter.reopen("BING", WORK / "bing.db", bus)
    resent = yi2.resend_unacked()
    print("重开后重传的 message_id:", resent)
    pump(hq2, yi2, bing2)
    # A second resend after delivery must be a pure no-op.
    yi2.resend_unacked()
    pump(hq2, yi2, bing2)
    seals = {sid: (s.holder, s.parent_batch) for sid, s in hq2.ledger.seals.items()}
    split_batches = [b for b in hq2.ledger.batches if b.batch_id == "B-SPLIT-OFFLINE"]
    print("断线拆并批次只登记一次:", len(split_batches) == 1)
    print("d1/d2 已就位:", "d1" in seals and "d2" in seals)
    show("重开后谱系关键状态", {"d1": seals.get("d1"), "d2": seals.get("d2")})
    hq2.close()
    yi2.close()
    bing2.close()


def scene_double_spend(bus: MessageBus) -> None:
    banner("场景 4：私拆未还又拿旧封整封归还 → 重复销号被拦截")
    hq = Counter("JIA", WORK / "ds-jia.db", bus, role="hq")
    bing = Counter("BING-DS", WORK / "ds-bing.db", bus, role="branch")
    hq.hq_dispatch("B-DS-0", "DS-0", "BING-DS", {"B": 2})
    pump(hq, bing)
    # BING secretly splits DS-0 into DS-1 (the physical coins move on) ...
    bing.request_split_merge(
        "B-DS-SPLIT",
        ["DS-0"],
        [{"seal_id": "DS-1", "holder": "BING-DS", "contents": {"B": 2}}],
    )
    pump(hq, bing)
    # ... then tries to hand the already-surrendered old seal back as well.
    bing.request_return("DS-0")
    report = hq.run_inbox()
    hq.run_acks()
    bing.run_acks()
    show("旧封二次交回的拒绝凭据", report)
    # And reconciliation still flags DS-1 as in transit (not written off).
    settle = hq.settle(DOMAINS, [])
    show(
        "核销报告（在途支线未销号）",
        {
            "ok": settle["ok"],
            "in_transit_seals": settle["in_transit_seals"],
            "problem_codes": [p["code"] for p in settle["problems"]],
        },
    )
    hq.close()
    bing.close()


def scene_global_conflict(bus: MessageBus) -> None:
    banner("场景 5：两份复称单查都可能，全程却没有同一解")
    hq = Counter("JIA", WORK / "gc-jia.db", bus, role="hq")
    yi = Counter("YI-GC", WORK / "gc-yi.db", bus, role="branch")
    bing = Counter("BING-GC", WORK / "gc-bing.db", bus, role="branch")
    hq.hq_dispatch("B-GC-0", "GC-0", "YI-GC", {"A": 1, "B": 1})
    pump(hq, yi, bing)
    yi.request_split_merge(
        "B-GC-SPLIT",
        ["GC-0"],
        [
            {"seal_id": "gc-a", "holder": "YI-GC", "contents": {"A": 1}},
            {"seal_id": "gc-b", "holder": "BING-GC", "contents": {"B": 1}},
        ],
    )
    pump(hq, yi, bing)
    # Each branch reports weight 1.  Individually legal (both domains contain 1),
    # but together with the original A+B=4 appraisal they have no common model.
    yi.request_reweigh("gc-a", (1, 1))
    pump(hq, yi, bing)
    bing.request_reweigh("gc-b", (1, 1))
    pump(hq, yi, bing)
    yi.request_return("gc-a")
    bing.request_return("gc-b")
    pump(hq, yi, bing)

    receipts = [
        {"receipt_id": "PL-原鉴定", "counts": {"A": 1, "B": 1}, "lo": 4, "hi": 4}
    ]
    report = hq.settle(DOMAINS, receipts)
    show(
        "全程相容性裁决（冲突凭据 = 一组已冲突的回单）",
        {
            "ok": report["ok"],
            "problem_codes": [p["code"] for p in report["problems"]],
            "weight_report": report["weight_report"],
        },
    )
    # A proposed mass table is likewise rejected with the exact receipts broken.
    fixed = hq.ledger.reconcile(
        DOMAINS, receipts, proposed_masses={"A": 1, "B": 1, "C": 2}
    )
    show(
        "持指定配重 A=1,B=1 的核验",
        {
            "satisfiable": fixed["weight_report"]["satisfiable"],
            "conflicting_receipts": fixed["weight_report"]["conflicting_receipts"],
        },
    )
    hq.close()
    yi.close()
    bing.close()


def main() -> int:
    bus, hq, yi, bing, store, office = fresh_workspace()
    try:
        scene_ticket_library(store, office)
        scene_weighing_conflict(office)
        happy = scene_happy_tour(hq, yi, bing)
        scene_restart(bus, hq, yi)
        scene_double_spend(bus)
        scene_global_conflict(bus)
    finally:
        store.close()
        office.close()
        for counter in (hq, yi, bing):
            try:
                counter.close()
            except Exception:  # noqa: BLE001 - already closed in restart scenes
                pass

    banner("演示完成")
    print(
        "全部记录位于 demo/tour/（票据库、天平室、三处柜台各一个 SQLite 库 + spool 邮件目录），\n"
        "无任何网络依赖；删除该目录后重跑 python3 museum_demo.py 可完整复现。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

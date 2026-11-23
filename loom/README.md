# 织样机扩展（loom）：实体织梭、装梭业务与执行带

织机扩展与试剂库存服务共用同一个 SQLite 库（`loom/schema.py` 的表由
`reagent.schema.initialize` 一并建立）。它不另存任何线量副本：梭里的每一份
余线都能追溯到 reagent 库的一次真实运行（`runs` + `reservations`）。

## 领域规则落地

**装梭 = 原有运行的预留 + 开始（两阶段）**

- `reserve_load`（开领/预留）：按梭颜色选同 `kind` 的瓶，自动建一份
  `loom-load:<颜色>:<份数>` 方法，用 `load_id` 作 `run_id` 调原 `create_run`。
  只写 `active` 预留，梭不上机。实领不足或瓶当日失效时整个事务回滚，
  梭里不会凭空出现线。
- `confirm_load`（实领）：调原 `start_run`，重新核对当日效期。
  - 成功：运行进入 `started`、预留转 `consumed`，梭按**出厂停靠边**上机，
    写一条 `load` 事件（+N 份）。
  - 失败（如蓝芯到期）：原运行服务释放全部预留并给出终态回执，装梭记为
    `failed`，梭保持 `registered` 不上机。
- 两阶段各自幂等：重发预留返回原请求；重发确认返回保存的终态回执，
  不会再实领一次。

**实体梭与只增事件账本（`shuttle_events`）**

- 梭的芯号、颜色、出厂边终身固定（`shuttles` 表约束 + 芯号唯一）。
- 余线 = 事件 `portions_delta` 之和（load 为正、pass 为 -1、其余为 0），
  与瓶的 `unallocated = total − reserved − consumed` 同样是推导值，不存计数。
- 事件类型：`load / unload / carry / pass / handoff`。`event_id` 即业务回执号，
  全局唯一；触发器禁止 UPDATE/DELETE。重发同一回执只回放、不再移动。
- `pass`（过梭）：机头与梭必须同侧；消耗一份，梭与机头同时换到对岸。
- `carry`（搬）：搬梭工把一只梭单独移到另一侧，消耗一张搬运票；
  不耗线、不动机头。过梭与普通换梭不耗票。
- `handoff`（整梭跨机交接）：只允许有余线的梭，侧不变、余线随行；
  线永远不退成瓶的未用量（consumed 保持不变）。
- `unload`：只允许余线为 0 的空梭，卸下后可重新装梭（再次回到出厂边）。

**交接点查询（无需织单在场）**

`shuttle_trace(shuttle_id)` 给出余线、在哪侧、在哪机，以及使它如此的
全部装/搬/过梭/交接回执链和每次装梭对应的运行终态。

## 织单与最少票执行带

选做题目中的**执行带**方案（整单带实际梭号）：

1. `create_weave_job` 调规划器 `planner.plan_tape`，按该机**当前**可见梭位、
   余线和纹样做 Dijkstra 搜索，代价字典序为 `(搬运票数, 动作数)`，
   因此首先保证票数最少，其次动作最少。超票或缺色直接拒单（不打一半）。
2. 编译出的每一步带具体 `shuttle_id`、侧和（过梭步）颜色，存入 `weave_steps`，
   预分配回执号 `<job>#evt#<i>`。
3. 正式开织前/每步行使前，`_simulate_tape` 用账本事实**逐步重放**整条未织
   后缀：机头位置由已完成过梭行数推导，梭侧/余量随模拟推进，并核对交接、
   颜色、同侧、余线和票额。排练时被别的机台交接走、被搬梭工挪过都会改判
   `failed`，且不耗任何线。
4. `weave_next` 每次只落一个事件；关机重启后已 `done` 的步骤不重复执行，
   从最后实际完成的行续织，回执重发幂等，故不重复耗线。
5. `void_job` 把整带作废（已完成行与已落事件保留可审计），再对未织后缀
   按当前实物重新 `create_weave_job` 即可续织。
6. 成品（`product_json`）同时记录纹样、行数、用票数和每只梭的余量去向。

题目中的另一方案（开机时按可见梭位下发“下一步控制表”）未实现；执行带
方案已覆盖其两个关键难点——整带作废后的后缀重编，以及实物绑定/已过行去重
（步骤绑定唯一梭号 + 回执号唯一 + done 不可变触发器）。

## 边界

单机至多 6 只梭（触发器）、纹样至多 16 行、票至多 4（规划器校验），
搜索带 20 万状态安全闸。业务日沿用 reagent 的非递减全局时钟。

## 演示与测试

```bash
python3 -m unittest discover -s tests -v   # 77 个测试（46 原库 + 31 织机）
python3 demo_loom.py                       # A-F 六个场景
```

CLI 子命令：`register-loom / register-shuttle / reserve-load / confirm-load /
carry / pass / handoff / unload-shuttle / shuttle-trace / create-job /
verify-job / weave-next / void-job / get-job`。

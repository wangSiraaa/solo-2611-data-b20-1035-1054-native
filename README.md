# 试剂库存服务与独立检验运行库 (719) + 封签/底模/货运处置层 (720)

Python 3.11 + SQLite 的试剂库存服务、运行库，以及建在其上的具名封签、
底模档案与货运站处置层。只使用 `materials/719`、`materials/720` 中的
合成试剂和题定规则；不生成实验建议。

## 运行

```bash
python3 -m unittest discover -s tests -v     # 80 个测试（46 试剂 + 34 封签）
python3 demo_719.py                          # 跑 719 操作文件并打印可手算守恒
python3 demo_720.py                          # 跑 720 封签/底模/货运全场景
```

## 720 封签/底模/货运层

模块在 `seals/` 下，复用 `reagent` 的运行状态机，不另存任何份数计数：

| 模块 | 职责 |
| --- | --- |
| `seals/labels.py` | 只有成功 `started` 的运行、其 `consumed` 预留行和份内序号才能压签 |
| `seals/matching.py` | 纯函数底模/印带还原：互补断纹、分段、废边凭据、反读、接带痕 |
| `seals/archive.py` | 底模/凭据登记、印带版本化、来历查询与证据输出 |
| `seals/freight.py` | 套装树归类与最低工时拆封/原扣/放行方案 |
| `seals/workflow.py` | 持久化交割工作流、破封回执、丢包重开与幂等处置 |

### 制签（`seals/labels.py`）

- 标签外键指向具体 `consumed` 预留行加份内序号；一份泥一条签带，标签数永远
  由已消耗行数推导，运行上没有私有计数器。
- 仅预留（`reserved`）、过期未开成（运行转 `released`）都拒绝压签；重复扫码
  返回原签并记一条 `duplicate` 审计，绝不多发。
- 拒绝事件在独立事务里先落 `label_attempts` 再抛错，失败扫描不随业务回滚消失。
- 重复 `start` 只回放 `runs.terminal_json`，不会再消耗一份。

### 底模还原（`seals/matching.py`）

- 相邻断纹逐格互补：印 `1` 只能盖模 `0`、印 `0` 只能盖模 `1`；`?` 是仍占一格
  的磨损格，两边都能配。
- 段内必须连续；两段之间只能凭当班废边凭据（`waste_vouchers`）跳过其区间内
  的齿，段内不许跳齿。
- 所有几何用递增模齿坐标表达；仅当定位孔整个脱落（`locating_hole=missing`）
  才允许整条反读。
- 接带痕是相邻齿之间的线：可读段覆盖到的每个邻接，底模与印带必须同时有痕或
  同时无痕，否则输出让该定位失效的具体证据。
- 同一底模同一段可被任意多只箱引用，不建任何唯一性约束。

### 档案与版本（`seals/archive.py`）

- 库存只压底模、发签用料和箱上实印条，绝不预填某箱装的是哪个炉批。
- 旧签新读出几格必须另出一版（`imprint_versions`），旧版保留；查询返回
  所有可能炉批、实模段、跳过的凭据位置，以及无解模的可见证据。

### 货运处置（`seals/freight.py`）

- 叶箱只有在「标签合法 + 至少一条可还原模段 + 可能炉批均不在点名名单」时才
  能放行；任一不满足即 `PlanBlocked` 停单，无法查证不等于清白。
- 方案穷举套装树上的两种选择：整柜原封扣回（一次收费、覆盖全部后代、后代不
  再计费）或付拆封工时逐件处理；海关押封柜只能原扣。
- 合同上限只统计被陪扣的**已证清白**叶箱；原扣柜后代在 `leaf_outcomes` 中
  只出现一次。

### 持久化交割（`seals/workflow.py`）

- 柜先模式：锁整柜 → 按批准方案逐层破封 → 责任转给子柜/叶箱。
- 破封回执先落库再回包；扫码器断电后 `close`/`reopen` 能认出已破具名封、已发
  走的箱和待办动作。重复破封、重复处置都是幂等回放，同一只箱不会既发客户又
  挂扣车。
- 箱先模式：叶箱登记具名责任 → 合并进可原扣外柜；未封成的合并可撤回，已封成
  的合并不可撤回，已发箱去向保留。

### CLI

```bash
python3 -m seals --db store.db mold-register   --mold-id mA --batch A --pattern 00 --day 12
python3 -m seals --db store.db voucher-register --voucher-id v1 --mold-id m4 --shift s1 \
                 --first-tooth 1 --last-tooth 2 --min-skip 2 --max-skip 2
python3 -m seals --db store.db label-issue     --label-id LX --box-id X --run-id r1 --day 11
python3 -m seals --db store.db imprint-record  --imprint-id IX --label-id LX --imprint read.json --day 12
python3 -m seals --db store.db reconstruct     --imprint-id IX
python3 -m seals --db store.db plan            --plan-doc plan.json
python3 -m seals --db store.db workflow --wf-op open --workflow-id w1 --day 13
```

规模约束：每案至多 20 条印签、每块底模 40 齿、3 层箱、两辆虚构货车，均有校验。

---

## 719 试剂库存服务

CLI 独立入口（`--db` 指向 SQLite 文件）：

```bash
python3 -m reagent --db store.db init --inventory materials/719/inventory.json

# 库存侧
python3 -m reagent --db store.db query       --bottle U --day 14
python3 -m reagent --db store.db snapshot    --day 14
python3 -m reagent --db store.db conservation
python3 -m reagent --db store.db open        --bottle U --day 11
python3 -m reagent --db store.db hold        --holder-id h1 --day 10 --bottle U=1
python3 -m reagent --db store.db consume-hold --holder-id h1 --day 12
python3 -m reagent --db store.db release-hold --holder-id h1 --day 12 --reason abort
python3 -m reagent --db store.db get-hold    --holder-id h1 --day 12

# 运行侧
python3 -m reagent --db store.db revise  --method-id M --revision 2 --requirement R=2 --day 12
python3 -m reagent --db store.db create  --run-id r1 --method-id M --method-revision 1 \
                                          --day 10 --bottle U=1
python3 -m reagent --db store.db start   --run-id r1 --day 12
python3 -m reagent --db store.db release --run-id r1 --day 12 --reason cancel
python3 -m reagent --db store.db get-run --run-id r1
python3 -m reagent --db store.db run-ops --inventory materials/719/inventory.json \
                                          --operations materials/719/operations.json
```

## 关键规则

**有效期**（`reagent/bottles.py`）

- 未开封：`intake_day ≤ day < printed_expiry_day`
- 已开封：`intake_day ≤ day < min(printed_expiry_day, opened_day + L)`
- 边界当天不可用；`L` 与印刷期只取更早者，不会互相延长。
- 开封日只能由“实际使用”或显式人工开封设置；库触发器 `bottles_open_immutable`
  禁止清空或改写已开封日。

**守恒与“可领”分离**（`bottle_allocation` 视图 + `reagent/bottles.py`）

- `unallocated = total − reserved − consumed`，永远由预留行推导，不另存计数。
- `reserved` 是状态为 `active` 的预留之和；`consumed` 是 `consumed` 之和；
  `released` 两者都不计，份数回到未占。
- `leadable_today = unallocated if 当日有效 else 0`。过期只是让份数不可领，
  不会把它从未占里抹掉。SQL 视图和 Python 推导在 `conservation` 中交叉核对。

**预留不是使用**

- `create` / `hold` 只写 `active` 预留，不设开封日、不消耗开封寿命。
- 只有成功的 `start` / `consume-hold` 才在同一事务内把 `active → consumed`
  并给未开封瓶写入本次实际使用日；已有开封日不重置。
- 失败请求整体回滚：不会给任何瓶留下开封事实。

**运行状态机**（`reagent/runs.py`）

- `create` 在一个 `BEGIN IMMEDIATE` 事务里校验方法需求 + 选择 + 当日效期，
  然后一次性写入全部预留；任何一步失败都不留半个运行或半组预留。
- `start` 重新核对当日效期，把该运行的全部 `active` 预留原子转成 `consumed`。
  过期导致失败时释放该运行的全部预留、记录原因，并使运行进入 `released` 终态。
- 终态运行不可再消耗、释放或返还；重复 `start` 返回**原样保存**的终态回执
  （`runs.terminal_json`），不会按之后的库存重算。
- 选择按种类合计必须恰等于方法需求，可同种分摊多瓶，不能多领、少领或用异种替代。

**库存事实只有一份**

- 预留行用通用 `holder_id` 标识；运行直接以 `run_id` 作 holder，所以运行视图
  中的数量是对同一批预留行的 `GROUP BY`，不存在第二份互不约束的计数。
- `tests/.../test_run_quantities_are_derived_from_inventory_rows` 直接改预留行，
  运行视图随之变化，证明没有副本。

**并发与身份**

- `BEGIN IMMEDIATE` 先拿写锁，配合 `busy_timeout` 串行化“最后一份”的争用；
  两连接中至多一方预留成功，另一方读到 0 未占后以 `InsufficientStock` 失败。
- 外部 holder 不能占用某运行的 `run_id`，运行也不能复用已存在的 holder，
  避免绕过运行状态机结算预留。

**方法修订**

- 新修订写入 `methods`/`method_requirements`，只影响之后新建的运行。
- 运行在创建时把需求快照写入 `run_requirements`（触发器保证不可改）；
  新需求仍受瓶的实际有效期约束。

## 719 结果（`demo_719.py` 输出）

| 操作 | 结果 |
| --- | --- |
| U 10 日预留 1 | 未开封，`reserved=1`，`unallocated=3` |
| U 12 日 start | 开封日=12，`consumed=1` |
| new-need (v2) 12 日 start | 再消耗 2，`consumed=3`，`unallocated=1` |
| U day 13 | `unallocated=1`，`leadable_today=1` |
| U day 14 | `unallocated=1`，`leadable_today=0`（开封 12 + L2 到期） |
| P 10 日预留 1，11 日 start | 失败并释放；`unallocated=2`、`reserved=0`、`consumed=0`、`leadable=0` |
| LAST 两连接争用 | 恰一方成功，另一方 `InsufficientStock`，胜者不设开封 |
| 重复 start reserve-U | 返回原终态，不再扣 1 |

守恒终态：`U 4=1+0+3`、`P 2=2+0+0`、`LAST 1=0+1+0`。

## 范围

至 100 瓶 / 200 运行已在 `ScaleTests` 覆盖。不做分布式事务、进程强杀、
真实仪器通信或结果判读。终态收束依赖单库事务 + 保存回执：开始失败时在同一
事务内释放全部预留并写终态原因，之后的重放只读该回执。

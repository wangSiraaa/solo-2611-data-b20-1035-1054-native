# gradaudit — 独立课程等价目录与毕业要求审核库

一个可外部接入的 Python 3.11 产品，包含三块相互独立、又共享同一套输入合同的组件：

| 组件 | 入口 | 作用 |
| --- | --- | --- |
| 课程等价目录 + 审核库 | `gradaudit.Store` / `python -m gradaudit ...` | SQLite 持久化目录、转入记录、要求、准备/确认/重开 |
| 精确求解器 | `gradaudit.solver.solve` / `gradaudit solve` | 判定整体可行，可行时给出具体分配；不可行时给出各组独立最大点数 |
| 独立验证器 | `gradaudit.verify.verify_plan[_document]` / `gradaudit verify` | **不调用求解器**，按输入逐条重核一个 `course-allocation/1` 方案 |

四个公开 JSON 文档的字段含义即题面合同。目录**只**通过显式 `equivalences` 映射给出候选，绝不按课程名猜测。

材料在 `materials/725/`，演示在 `examples/demo.py`，测试在 `tests/`。

```bash
python3 -m unittest discover -s tests -v     # 64 个测试
python3 examples/demo.py                      # 全流程演示（审核库）
python3 examples/credit_demo.py               # 全流程演示（可转学时底单 / 换章窗口 / 停窗 / 交接）
```

---

## 1. 输入合同（公开字段）

### 目录 `course-catalog/1`
```json
{
  "format": "course-catalog/1",
  "revision": 1,
  "courses": [{"course_id": "LC_BOTH", "categories": ["CORE", "OPT"]}],
  "equivalences": {"Z": ["LC_BOTH"]}
}
```
* `courses[].categories` 是每个本地课程的类别集合。
* `equivalences[外部课程] = [本地课程ID...]` 是全部候选；一个外部课程可映射到多个本地课程。
* 候选**只能**由此映射得出；不在此映射中的外部课程没有候选。

### 转入记录
```json
{"student_id": "ST1", "revision": 1,
 "records": [{"record_id": "R1", "external_course": "X", "points": 3}]}
```
* `record_id` 稳定、唯一；`points` 为正整数（合同规模 1..6）。

### 要求
```json
{"request_id": "REQ1", "revision": 1,
 "groups": [{"group_id": "G_CORE", "allowed_categories": ["CORE"], "minimum_points": 3}]}
```
* 至多 3 组；每组给出允许类别集合与最低点数（正整数）。

### 方案 `course-allocation/1`
```json
{"format": "course-allocation/1", "student_id": "ST1",
 "catalog_revision": 1, "records_revision": 1,
 "request_id": "REQ1", "requirements_revision": 1,
 "assignments": [{"record_id": "R1", "course_id": "LC_CORE", "group_id": "G_CORE"}]}
```
* 固定身份五元组；`assignments` 每项只给 `record_id/course_id/group_id`。
* **点数一律从记录读取**，方案自报的点数字段被忽略。
* 未出现的记录按“未使用”处理；允许额外说明字段，不依赖字段顺序。

---

## 2. 问题模型

每条记录至多被选一个候选本地课程，并**完整**分配给至多一个组，不能拆点、不能跨组复用；可整体不用。选中课程至少有一个类别命中该组允许类别，才可分配到该组。各组可超额满足，解不要求唯一或最少使用记录。

* 表示层（`model.py`）把记录、候选、类别、点数全部解析成不可变对象。
* 求解器（`solver.py`）做精确判定并给出一个可行分配。
* 验证器（`verify.py`）**独立**重算：身份 → 候选 → 类别 → 点数去向 → 各组达成。任何满足合同的方案都接受，哪怕与求解器给出的不同。

---

## 3. 求解器：状态与成本

状态为 `(pointer, gains)`：

* `pointer`：下一条待决定的记录下标（记录按 `record_id` 排序，每条用一次或跳过）。所有 `< pointer` 的记录已决定，因此不需要额外的 used 位掩码。
* `gains`：各组点数，**截断到该组最低分**。截断不改变可行性，却能把“已满足同一组集合”的部分解合并成同一状态。

成本：不同 `gains` 向量数 ≤ `∏(min_g+1)`。3 组、每组最低分 ≤6 时 ≤ 343，于是状态数 ≤ `n·343`（`n ≤ 12`，约 4k）。每个状态再枚举该记录的剩余候选课程与组，即
`O(n · ∏(min+1) · (1 + Σ|候选|) · G)`。**多候选只增加转移、不增加状态；点数多样性只拓宽被截断的 gain 网格。** 这是精确 DP，可证明不可行，不是启发式。

不可行时，`groups[].max_ignoring_other_groups` 给出“忽略其它组竞争时该组能达到的最大点数”（真实总和，不截断）。例如修订 2 下 `CORE=8, OPT=2`：两组各自都够，但同一份记录不能同时计入两组，故整体不可行——单组可达**不代表**整体可行。

```bash
python3 -m gradaudit solve --catalog materials/725/catalog.json \
  --records materials/725/records.json --requirements materials/725/requirements.json \
  --emit-plan /tmp/plan.json          # 退出码 0=可行 1=不可行
```

---

## 4. 独立验证入口（verify-plan）

```bash
python3 -m gradaudit verify --plan materials/725/plan_a.json \
  --catalog materials/725/catalog.json \
  --records materials/725/records.json \
  --requirements materials/725/requirements.json
```

`verify` **不导入求解器**，也不与求解器选中的方案逐项比对。它按输入重新核：

| 情形 | 结果码 |
| --- | --- |
| 方案身份/修订与输入不符 | `identity_mismatch` / `revision_mismatch` |
| 记录不存在 | `unknown_record`（定位到 record_id） |
| 同一 record 出现多次（复用/拆点） | `duplicate_record`（定位到 record_id） |
| 课程不是该外部课程的候选 | `course_not_candidate` |
| 课程类别不命中该组 | `category_mismatch` |
| 组点数不足 | `group_underfilled`（带 achieved/minimum） |
| 未知组 | `unknown_group` |

退出码：`0` 接受，`1` 拒绝，`2` 输入错误。任何满足全部合同的方案都接受；缺省记录按未使用。

**两份由题面独立给定、分配不同的合法方案**（`plan_a`、`plan_b`）都被接受；`plan_invalid_reuse` 复用 R1，被拒绝且原因定位到 `R1`。新生成的方案（`solve --emit-plan`）也走同一入口并接受。

---

## 5. 审核库：准备 / 确认 / 重开

```python
from gradaudit import Store
store = Store("gradaudit.sqlite3")
store.import_catalog(doc); store.import_records(doc); store.import_requirements(doc)

prep = store.prepare_audit(plan_doc, request_id="REQ1",
                           catalog_revision=1, records_revision=1, requirements_revision=1)
store.confirm_audit(request_id="REQ1")
store.confirmation_status("REQ1")      # -> confirmed / stale
store.reopen_audit(request_id="REQ1", reason="...")
store.history("REQ1")
```

### 修订与导入
* 每个目录 / 记录 / 要求修订都以 **内容哈希 + 原文** 落库。
* 相同修订号、不同内容再次导入 → `RevisionConflictError`（**拒绝**，不得把新映射冒充旧修订）；相同内容重复导入幂等。
* 旧修订始终可查（`get_catalog(1)`、`list_candidates(1, "X")` 等）；映射修订真实改变下一次可行性（修订 2 后 OPT 只能取 2 点，整体不可行）。

### 准备（prepare）
* 固定当时的 `(catalog, records, requirements)` 修订。
* 用**同一个独立验证器**核方案（不限求解器产物，外部方案同样走身份核对与确认边界），并持久化每条记录的**点数去向**（`preparation_points`）。
* 被拒方案也会作为“已拒绝的准备”留痕，且永远不能确认。

### 确认边界
* 只有被独立验证器**接受**的准备才能确认。
* 确认时要求“当前有效修订”仍等于准备时固定的修订。当前有效修订取：最新目录修订、该请求最新要求修订、该学生最新记录修订。**只要准备后目录 / 要求 / 学生记录发生变化，旧方案即 `stale`，不能继续确认**，必须重新准备（`ConfirmationStaleError`）。
* 确认**不删除**任何原记录：它只新增确认行，保留原准备、原方案、使用的修订。
* 重开（reopen）记录显式事件、清除“当前确认”状态，但全部历史（准备、确认、事件）保留，可回查所有点数去向。

### 主要表
`catalog_revisions / catalog_courses / catalog_course_categories / catalog_equivalences`、
`records_revisions / transfer_records`、
`requirements_revisions / requirement_groups / requirement_group_categories`、
`audit_requests`、`audit_preparations / preparation_points`、
`audit_confirmations`、`audit_events`。

---

## 6. 目录维护

```bash
python3 -m gradaudit import-catalog materials/725/catalog.json
python3 -m gradaudit import-catalog materials/725/catalog_revision2.json   # 修订 2
python3 -m gradaudit list-candidates 1 X    # 独立列出候选及类别，不替学生选择
```

---

## 7. 测试覆盖

`tests/test_gradaudit.py`（31 项）覆盖：候选显式不猜测、点数校验、组数上限；两份合法方案均接受、复用被拒且定位 R1、未知记录/非候选/类别不符/修订不符、点数只从记录读取、未用记录、额外字段；两组争一份、单组够整体不够、多候选与未分配、精确不可行的共享记录；修订冲突、旧修订可查、映射改变可行性；准备-确认-重开、拒收准备不可确认、仅导入新修订也会使确认 stale、外部方案同边界；以及 **200 个随机小实例与暴力枚举的可行性一致性交叉校验**（并校验每个可行解都能被验证器接受）。

规模合同：至 12 份记录、每份至多 3 候选、点数 1..6，均在 `solver.py` 中显式校验。不做排课、GPA 或先修。

---

## 8. 可转学时底单 / 换章窗口 / 停窗 / 离线交接

在已确认的分配之上另立一张**可转学时底单**，并检查有向换章窗口网络能否把真实面额滚大（多发），以及如何用最少的停窗阻断、同时保住必须可达的伙伴校种类。全部比例、余数、逐站计值都用精确 `Fraction`，绝不使用浮点。

### 协议档（`credit-protocol/1`）
按**实际选中**的本地课程查档，取得发行学校、可转学时种类与兑换比例：
```json
{"format":"credit-protocol/1","revision":1,"entries":[
  {"course_id":"PU_A1","issuing_school":"PU","kind":"A","ratio":"3/2"}]}
```
比例可为 `3/2`、`2`、`[3,2]` 或 `{"numerator":3,"denominator":2}`；**浮点被拒绝**（`rationals.parse_positive_rational`）。

### 底单（`ledger.py` / `Store.build_ledger`）
* 只从**已确认**方案开单；未用记录不开线；同一记录不得重复（不拆点）。
* 同 `(发行校, 种类)` 的已用记录先各以精确分数计时，再**合池向下取整一次**成可盖章学时。
* 不足一整点的余数**留在本池**（`remainder` + `REMAINDER::校::种类` 凭证），**不可挪到另一种类**——两池各 1/2 余数合计是一整点也不盖章。
* `appeal_lines()` 逐条给出：实际课程去向、归入哪一池、尾数凭证。
* `Store.build_ledger` 用确认时固定的 `(catalog, records, requirements)` 修订，故合法的校外分配按其**实际确认版本**办理。

```bash
python3 -m gradaudit build-ledger --request REQ1 --protocol-revision 1
```

### 换章窗口与多发（`windows.py`）
每个窗口只接写明「校+种类」的**整份**凭证，按正有理比例向下取整一次，盖为另一校一种；结果为 0 不出新凭证。一次查验从底单真实池出发，不拆并、不补点，最多 `max_steps` 步；回到**同校同种类且面额严格增加**即存在可走通的多发路径。

* `find_multi_issue` 逐站模拟取整，因此「比例连乘 > 1 但真实面额太小、逐站取整后走不成」的路径**不会**误报（例：`(3/2)·(3/2)=9/4>1`，但 1 点起手 `1→1` 不增）。
* 规模：≤8 种节点、≤10 窗、`max_steps`≤6。

### 停窗（`solve_suspension`）
求**关闭数最少**的一组窗口，使这批真实底单全部不能多发，同时 `must_stay_reachable` 的伙伴对仍沿开放窗口**连通**（只核接口可达）。`non_suspendable` 指定的联办窗口不可停。若保通与防多发不兼容，返回 `contradiction` 并分辨是「连通本身不可能」还是「连通与防多发冲突」。

```bash
python3 -m gradaudit suspend --windows w.json --starts s.json --charter c.json
```

### 离线交接（`handover.py`）
采用**去名批次清册 + 联盟覆盖回执**：学校把实际发行的 `(校, 种类, 整点面额)` 制成去名清册（相同面额合并、保留在校回查号）；联盟据此逐窗试盖，给出真实多发路径或逐面额覆盖。面额是否多发只取决于 `(校, 种类, 面额, 开放窗, 步数)`，与持有人身份无关，故**校内申诉与联盟听证对每一面额裁决一致**（`compare_ledger_and_roster`）。

* 补发 = 加一名持有人；撤单 = 去一名持有人，最后一名撤单后该面额不再发行、清册行消失，回执随之收缩。
* 听证后即使两端离线，仅凭交接件（`build_packet`/`review_packet`）即可复核：要么查出一次真实多发，要么确认停窗后覆盖本批所有已发行面额。
* 换版：`diff_judgements` 比较两个窗口修订下的逐面额裁决，给出**仍适用**、**新增多发**、**由多发改覆盖**、增删项，明确哪些旧判定继续有效。

```bash
python3 -m gradaudit handover --roster r.json --windows w.json --max-steps 2 --closed-window W_BA > packet.json
python3 -m gradaudit review-packet packet.json
python3 -m gradaudit coverage --roster r.json --windows w.json --max-steps 2 [--closed-window W_BA]
python3 -m gradaudit diff-windows --roster r.json --old-windows old.json --new-windows new.json --max-steps 2
```

### 题面场景（见 `tests/test_windows_ledger.py` 与 `examples/credit_demo.py`）
* 浦校两条各 1 点、均选 A 类、各 3/2 → 合并 A 类 3 点。
* A→河校 B 按 2/3、B→A 按 2：`3→2→4` 两步多发；停回窗 `W_BA` 后 A 仍可达 B，仅停 1 窗。
* 只把第二条改投 B 类：A=1、B=1；A 去河校 `floor(2/3)=0` 归零，B 无窗口 → **不必停窗**。
* 恢复原底单、把回窗列为不可停且仍要求 A→B：**无合规停窗方案**（矛盾）。
* 比例连乘 > 1、逐站取整 + 面额太小走不成：1 点起手不增，2 点起手才 `2→3→4`。

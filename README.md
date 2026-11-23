# 独立源段目录 + 多语言交付仓

Python 3.11，SQLite。**两侧物理分离**：源段目录与交付仓各自持有一个 SQLite
数据库；交付侧通过只读连接（`mode=ro`）真实读取源目录**已发布的源产品**，
不把位置编号当身份，也不缓存源状态。文本全部为合成内容，不做自动翻译或语义评估。

## 快速运行

```bash
# 端到端跑完 materials/753 全部场景（含用只读客户端读新旧包）
python3 run_demo.py

# 规则实演席：跑完全部规则局面（含勘误、预算、隐藏态、试错合并、坏册、改修订）
python3 -m delivery_system rules-demo

# 仅验证真实包文件
python3 materials/753/package_reader.py data/packages/PACK1.json
python3 materials/753/package_reader.py data/packages/PACK2.json

# 规则席命令行（数据在 materials/rules/*.json）
python3 -m delivery_system rule-check  --volumes materials/rules/base_four.json
python3 -m delivery_system rule-plan   --volumes materials/rules/base_four.json
python3 -m delivery_system rule-play   --volumes materials/rules/base_four.json --actual 丁
python3 -m delivery_system rule-replay --volume <one-volume.json> --keys 试色,转

# 测试（标准库 unittest，无需 pytest）
python3 -m unittest discover -s tests -v
```

## 目录结构

```
delivery_system/
  models.py              不可变值对象：SourceSegment/SourceProduct/SourceRef/
                         RuleVocabulary/TranslationEntry/CoverChunk/CoverDiagnosis/CoverResult
  source_catalog.py      源段目录（独立 SQLite + 发布快照 + release 历史）
                         + PublishedSourceReader（交付侧只读句柄）
  coverage.py            条目评估 + 精确覆盖区间 DP + 失败诊断
  delivery_repository.py 译文条目、覆盖计划、不可变发布包（独立 SQLite）
  package_io.py          translation-package/1 写入与结构/覆盖校验
  cli.py / __main__.py   命令行
  demo.py                materials/753 端到端复现（调用官方 package_reader.py）
  rules.py               规则实演引擎：Clause/Volume/State、首条适用条文、
                         白锁、完整性与可达局面
  plan.py                自适应鉴别脚本合成（AND-OR 搜索、合并检测、
                         失败证书、实演席 run_script）
  rule_materials.py      甲/乙/丙/丁、勘误、隐藏态对、试错陷阱、坏册等样例
                         + 修订绑定的整册装配 + JSON 互换
  rules_demo.py          规则实演席端到端复现（全部局面 + 装配/勘误/旧回放）
  cli.py / __main__.py   命令行
tests/test_delivery.py   20 个测试：覆盖、过期、重组、不可变、规模
tests/test_rules.py      29 个测试：引擎语义、两步鉴别、勘误、预算、隐藏态、
                         试错合并、互斥首键、坏册筛查、修订装配、JSON 往返、交接证明
```

## 两侧的实质逻辑

### 源段目录（写入侧 + 只读快照）
- `segment_id` 是身份；`position` 只是当前序位，重排只改位置不改身份/修订。
- 每段有独立 `revision`。`revise_segment` 只增修订、不改旧修订号——不存在
  “把旧译文贴上 2 号修订”这一动作的入口。
- 每次结构性变更（建/增/删/改/移）都写一条 `source_releases` 快照，
  `latest_release_id` / `product_at_release` 可回溯历史源状态。
- `PublishedSourceReader` 用 `file:...?mode=ro` 打开，每次调用重新读取，
  交付侧无法写源库；它读到的是**当前已发布**的源产品，因此源更正立即生效。

### 交付仓（选择侧 + 产品侧）
- 译文条目精确存储 `(segment_id, revision)`；`add_translation` 对同 id
  不同内容直接拒绝（幂等重放相同内容除外），无法改标签冒充补译。
- `assess` 把每条当前语言条目判为：
  `current_usable` / `stale_revision` / `non_contiguous` / `unknown_segment` /
  `malformed` / `empty_text` / `other_language`。**只有 `current_usable` 可入选**。
  过期/不连续/重复引用/空文本一律留作历史，不是当前候选。
- `plan_cover` 在**区间**上做可达性动态规划：起点 0 能否沿合法连续区间一步步到达 n。
  这正是不被“并集覆盖”忽悠的关键——重叠条目可能覆盖并集却无法按序不重不漏拼装。
  任意合法完整覆盖即可（不要求唯一/最少条目），按 id 字典序确定性地选一条。
- 生成时强制不变量：选中 `source_ids` 拼接必须精确等于源序；任一块文本非空；
  整体渲染文本非空。**无法交付时绝不产出空文本成功包。**

### 失败诊断（可交付性依据）
- `no_current_cover`：某些段没有任何当前可用译文，给出具体 `uncovered_segment_ids`。
- `no_exact_cover`：并集覆盖全部段，但有序精确覆盖不存在；给出
  `blocked_boundary`（装配卡住的位置）与 `crossing_translation_ids`（跨该边界、
  又不能拆开的条目），以及被排除条目及原因。FR 的 F12/F23/F4 即此例。
- `empty_source`：空源拒绝发布空包。

### 发布包（translation-package/1）
固定 `package_id/document_id/language/source_snapshot(segment_id+revision)` 及按输出顺序的
`chunks(translation_id/source_ids/text)`，另加 `source_revisions/rendered_text/
content_sha256/created_at/source_release`。包一旦写入即不可变：同 id 不同内容
抛 `PackageExistsImmutable`；旧包的原文快照与译文 id 永不改写（PACK1 保持 rev1）。

## materials/753 复现结果

| 步骤 | 结果 |
|---|---|
| EN 初始发布 PACK1 | 合法覆盖 `T1+T23+T4`（或 `T12+T3+T4`，二者皆合法） |
| FR 尝试发布 | 拒收：`no_exact_cover`，并集含四段但 F12/F23 在 S2 重叠且不可拆 |
| 更正 S2→rev2 | T12/T23/F12/F23 变 `stale_revision`；EN 缺 S2 (`no_current_cover`) |
| 补 T2_NEW(rev2) | EN 重建 `T1+T2_NEW+T3+T4`，发布 PACK2（S2@2） |
| 读旧包 PACK1 | 仍为 S1@1..S4@1 与旧译文 id/文本 |

`package_reader.py` 被以子进程方式真实读取新旧两个包并校验输出。

## 规则实演席（试玩桌上不能重开局）

译本是**同一源稿的多版规则册**；盒子出厂固定按其中一版工作，试玩全程不能重置成
初局。任务：在不重置的前提下，用一串按键 + 看到的公开图案，停手时准确报出实际用的
是哪一册。

### 源段目录侧：修订登记可用词汇
- 每个源段修订可登记 `RuleVocabulary`（`flags` / `visible_flags` / `patterns` / `keys`），
  即该修订允许的**局面标志、公开图案、按键**。`segment_vocabulary` 以
  `(document_id, segment_id, revision)` 为主键，修订一变旧词表仍可回溯，但**不能**拿
  旧修订号再登记到新版段——`set_segment_vocabulary` 会因修订不符抛 `RevisionConflict`。

### 交付仓侧：每册的真实译块 = 有限自动机
- `Volume`：按**整册实际块序 + 块内次序**排列的 `Clause`，加上标志、公开子集、按键、
  初局。
- 每次按键取**第一条适用条文**（按其块序）；`rewrite` 读的都是按键**前**局面；无适用
  条文则该键不答——`check_completeness` 把它记为**缺口**，并给出从初局可走到该局面、
  再按缺键的**按键历程**。
- “答白并进入永远报白”的锁局用 `locks=True` 条文建模（硬件行为，锁后每个键都答白，
  故不算缺口）。
- `replay(volume, keys)` 是**校译席**：只拿一册 + 一串按键，逐步得到公开图案、实际命中
  条文、内部局面；`complete=False` 即中途撞上缺口。

### 试玩席：一桌一只盒，合成单一分支脚本
- 候选 = 经 `screen_candidates` 剔除（缺答复、超规模）后的**合法册**；退件绝不进入候选。
- `synthesize`：对 `(册号, 局面)` 信念集做 AND-OR 搜索，**键由我方选、观察由对方出**，
  要求每个观察分支都可解 → 得到**确定性、看图案才决定下一键**的分支脚本；按预算迭代
  加深取最小步数。
- `synthesize_merged`：先**各册内部**把“未来按键反应完全相同”的局面合并（Moore 最小化）
  再搜；册号始终保留，故仍能报对书、单册勘误只失效该册的商。样例断言两者同深同脚本。
- 失败时 `PlanResult.failure` 给**证书**：`indistinguishable_volumes`（存在任意长走法都
  分不开的册组，如改丁后丙丁同报星）或 `budget_exhausted`（逐首键列出仍被合并的分支）；
  预算不够时尚未被合并的各册仍各自合法。
- `run_script(strategy, machine)` 是**实物试玩**：只凭 `press` 返回的图案决定下一键，绝不
  读内部标志；`verify_plan` 是**第二位馆员**：沿每条分支重走信念，核对脚本对全部候选
  均可跑通、叶节点恰好唯一命名一册（交脚本时附完整试走证）。

### materials/rules 复现结果（`python3 -m delivery_system rules-demo`）

| 局面 | 结果 |
|---|---|
| 甲乙丙丁 | 两步足矣：`试色`→红则`转`（方/圆）、蓝则`推`（星/月），四册各自命中 |
| 丁改判（推报星） | 丙丁**永远同反应**，证书 `indistinguishable_volumes` |
| 恢复旧册、预算=1 | 四册各自合法，却 `budget_exhausted`：第一键红/蓝各留两册 |
| 隐藏态对（戊己） | 前两键同为 红、方，第三键 `推` 才分 星/月（3 步） |
| 试错陷阱（壬癸） | `转` 可分；先按 `推` 两册同时锁白，此后**永久合并** |
| 互斥首键（子丑寅） | 每对都有鉴别键，但没有单一首键能一次分三册，需两步自适应 |
| 坏册（庚） | `推` 缺答复 → `screen_candidates` 以 `missing_answer` 退件，不入候选 |
| 修订/勘误/旧回放 | 改 `辨纹`→rev2：旧译块被判 `StaleBlockError`；只换丁为合译新块（`replace=True`），甲乙丙原封不动；旧册仍按旧规则回放 |


## 规模与不扩平台

测试覆盖 100 源段 / 5 语言 / ≥500 译文（含大量重叠诱饵），验证每语言精确覆盖、
诱饵从不入选、更正后按缺口隔离、补译后恢复。覆盖是 O(n·k) 区间 DP，
足以支撑题目规模；未引入翻译、语义评分、账号等平台能力。

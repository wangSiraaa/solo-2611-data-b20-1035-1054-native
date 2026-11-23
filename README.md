# 独立源段目录 + 多语言交付仓

Python 3.11，SQLite。**两侧物理分离**：源段目录与交付仓各自持有一个 SQLite
数据库；交付侧通过只读连接（`mode=ro`）真实读取源目录**已发布的源产品**，
不把位置编号当身份，也不缓存源状态。文本全部为合成内容，不做自动翻译或语义评估。

## 快速运行

```bash
# 端到端跑完 materials/753 全部场景（含用只读客户端读新旧包）
python3 run_demo.py

# 仅验证真实包文件
python3 materials/753/package_reader.py data/packages/PACK1.json
python3 materials/753/package_reader.py data/packages/PACK2.json

# 测试（标准库 unittest，无需 pytest）
python3 -m unittest discover -s tests -v
```

## 目录结构

```
delivery_system/
  models.py              不可变值对象：SourceSegment/SourceProduct/SourceRef/
                         TranslationEntry/CoverChunk/CoverDiagnosis/CoverResult
  source_catalog.py      源段目录（独立 SQLite + 发布快照 + release 历史）
                         + PublishedSourceReader（交付侧只读句柄）
  coverage.py            条目评估 + 精确覆盖区间 DP + 失败诊断
  delivery_repository.py 译文条目、覆盖计划、不可变发布包（独立 SQLite）
  package_io.py          translation-package/1 写入与结构/覆盖校验
  folding.py             纸条折叠引擎：正反层序、左/右合拢、折层凭单
  fold_operations.py     折法绑定译块的操作标（旧标不可移用）
  needle_mold.py         可调针模、穿孔仿真、校样反推、订单判定
  handoff.py             封样后的校准交接（清册 / 脱敏条件两种）
  calibration_demo.py    长条窗花读样 + 针模校准端到端复现
  cli.py / __main__.py   命令行
  demo.py                materials/753 端到端复现（调用官方 package_reader.py）
tests/test_delivery.py   20 个测试：覆盖、过期、重组、不可变、规模
tests/test_calibration.py 36 个测试：折叠、操作标、校准、订单、共轴、交接
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

## 长条窗花读样 + 可调针模校准

纸条分成 2 的幂个等宽格，每格有稳定名字，正反两面分清。源段版本上登记折纸方向与
可折位置；译者为实际采用的译块交操作标（从当前纸幅左/右半提起、翻过中线压到另一半）。

### 折叠与读样（folding.py）
- `FoldPlan(cells, directions)`：`L` 把左半提起翻到右半，`R` 反之。被提起的一摞
  整摞上下顺序与各格朝上面都翻转，合拢后从桌面左边重数位置。
- 对 `A B C D` 左折后：左针从上至下 `B, C`，右针 `A, D`；右折后左针先碰 `D`。
- `FoldVoucher` 对任一合法中间步出折层凭单：可按针位逐层问到原格与朝向
  （`layers_at_needle`/`by_needle`），也可按原格问当前位置与层深（`locate`）。

### 操作标（fold_operations.py）
- 操作标绑定到具体译块和精确 `(segment_id, revision)`。源修订过期、译块不再属于
  当前采用的精确覆盖、或折法/可折位置不符时判为不可用——**旧操作标不能移用**。

### 针模与校准（needle_mold.py）
- 每个台面针位固定接一个调节轴，同轴各针打相同层数；每轴给候选深度，0 停针；
  机器从顶向下穿到调定层数，过底不再穿，未被纸覆盖处不留痕。
- `compatible_settings` 要求**全部校样由同一副设置解释**；无解即校准自相矛盾。
- `judge_order` 在**所有相容设置**上判定订单：
  `go`（全部符合）/ `hold`（有的成有的坏，附两份设置与展开图）/ `return`
  （全不符合）/ `contradiction`（校准本身无解）。
- `naive_shared_axis_verdict` 演示反例：两针共用一轴时，按各自有利深度拼凑会
  误放行；真正判定必须守住同轴同深度。

### 校准交接（handoff.py）
- `roster`：封样前展开各张折层，签出**所有相容设置清册**。工头可直接拿其中一副
  打样；各轴独立时清册可能很大，重新校准必须换发清册。
- `conditions`：按实测原格反追翻折，只交**去掉客户标识的轴深条件、整组孔数与
  折样几何**（格名换成不透明标签），保留组数与共轴关联。工头仅凭它找一个能交和
  一个不能交的实际设置。
- 两种交接都能在**原校样撤走后复做订单**；校准证据自身矛盾时两种都拒发（停工）。

### 复现（materials/strip + calibration-demo）

```bash
python3 -m delivery_system calibration-demo
python3 -m delivery_system calibrate --calibration materials/strip/calibration.json
python3 -m delivery_system judge-order o1 --cells A,B,C,D --fold L --steps 1 \
    --punch B --intact C --calibration materials/strip/calibration.json   # hold
python3 -m delivery_system seal-handoff --calibration materials/strip/calibration.json \
    --mode conditions --out /tmp/h.json
python3 -m delivery_system judge-order o2 --cells A,B,C,D --fold R --steps 1 \
    --punch B --intact C --handoff /tmp/h.json                             # return
```

| 场景 | 结果 |
|---|---|
| 校样 B 有孔，左针 1/2 深、右针停 | 相容设置 2 副 |
| `o1` 左折 B 有孔、C 完整 | `hold`（暂缓；1 层深可交、2 层深不可） |
| `o2` 仅换右折（校准不变） | `return`（左针先碰 D，任一设置都交不出） |
| `o3` 保留左折、C 不限 | `go`（两种设置均交得出） |
| 两针共轴、各自拼凑深度 | 朴素法误判 `go`，真正判定 `return` |
| 校样自相矛盾 | 两种交接均拒发（停工） |


## 规模与不扩平台

测试覆盖 100 源段 / 5 语言 / ≥500 译文（含大量重叠诱饵），验证每语言精确覆盖、
诱饵从不入选、更正后按缺口隔离、补译后恢复。覆盖是 O(n·k) 区间 DP，
足以支撑题目规模；未引入翻译、语义评分、账号等平台能力。

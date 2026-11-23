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
  cli.py / __main__.py   命令行
  demo.py                materials/753 端到端复现（调用官方 package_reader.py）
  crossword/             双语纵横字谜层（见下节）
tests/test_delivery.py   20 个测试：覆盖、过期、重组、不可变、规模
tests/test_crossword.py  33 个测试：谜面册、造词单、拼字盘、离线交接
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

## 双语纵横字谜层（crossword/）

在既有「源段目录 / 交付仓」分离之上再加两个库，四库物理分离：

```
crossword/
  models.py        不可变值对象：ClueSense/Root/SuffixClass/Ban/CoinageSheet/
                   CoinageEntry/Credential/SpellingSource/ClueAnswers/
                   Slot/BoardDefinition/BoardSolution/Conflict/BoardAnalysis
  clue_registry.py 源侧谜面册：每谜面钉在确切 (segment_id, revision)；词义号、
                   词根、可用词尾类别、经审定根尾禁例。PublishedClueReader 只读句柄
  repository.py    交付侧造词仓：造词单绑定确切 (package_id, block_id)，禁跨包复用；
                   撤稿留痕、已送印 proof 不可变
  answerbook.py    两侧拼合：逐条造词 → 每义项可填全字串 + 逐拼法回查凭据；
                   copy 词根按「同一谜面」作用域；禁例在此也拦截
  board.py         试盘求解：unique/ambiguous/contradictory、强制格、两份可见不同
                   完整盘、最小冲突锁/槽；可重开盘（save/load + 擦锁重排）
  handoff.py       两种离线交接：WordFormBook（逐词词形册）与 RuleWalker
                   （逐字规则 + 编号根尾批条，支持复写/禁配/撤规则改排）
  demo.py          `python run_demo.py --crossword` 端到端复现
```

### 关键不变量
- **谜面绑定修订**：造词单指向的谜面必须由该译块确实覆盖——同段且同
  `revision`（译块冻结的源修订）。同一天另一份包正文相同但块号不同 → 不能沿用旧单
  （`assert_block` / 重新 `add_sheet` 到新块）。
- **词根/词尾/字面全部明给**：不做自然语言推断。`root`/`copy`（同一谜面内复写
  刚选过的词根）/`literal`（补字面字符）+ 从登记类别取的 `suffix`；禁例在造词与
  拼册两处都拦。
- **同形异源仍是一张盘**：同一可见拼法有多条凭据时合并为一个 `SpellingSource`，
  求解器只按可见字面推理，故印出来仍是一张盘；撤掉一条凭据后若另一条仍在，该词保留。
- **回查稿源**：任选拼法都能给出「哪张造词单、哪个块、哪个词根/词尾/字面」的凭据。
- **离线**：试盘可 `save`/`load`，`xw-board --erase/--lock` 无需联网即可擦字、改锁、
  判唯一或重印；周一撤稿进下一校次，旧 proof 保留原稿源。

### 试盘判据
- 每格在所有完整合法盘面中的取值集合由定向可满足性查询得到：单值即**强制格**。
- 全格皆单值 → `unique`，给出完整答案盘；否则 `ambiguous`，给两份**确实可见不同**
  的完整盘及差异格；无解 → `contradictory`，给出最小不可同时满足的锁字或槽位。

### 规模
各校次至多 12 槽、每槽至多 10 格；求解为带 MRV 的回溯 + 逐格可满足性查询，
在该规模内即时完成。

```bash
python3 run_demo.py --crossword          # 端到端，22 项自检
python3 -m unittest discover -s tests -v # 含 33 项纵横字谜测试
```

## 规模与不扩平台

测试覆盖 100 源段 / 5 语言 / ≥500 译文（含大量重叠诱饵），验证每语言精确覆盖、
诱饵从不入选、更正后按缺口隔离、补译后恢复。覆盖是 O(n·k) 区间 DP，
足以支撑题目规模；未引入翻译、语义评分、账号等平台能力。

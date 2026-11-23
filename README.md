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

# 字序调查 + 装订：自行发布真实包并跑全部场景
python3 -m delivery_system collation-demo

# 用真实包文件跑一个场景（旧表 + 散页）
python3 -m delivery_system collation-run \
  --spec materials/collation/scenario_3_loose_page.json \
  --package materials/collation/packages/PKG_OLD_AB.json \
  --package materials/collation/packages/PKG_NEW4.json

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
  collation/             铅字字序调查 + 装订复排（见下节）
    glyphs.py            字模表、最长匹配分字、词头比较
    marks.py             校印标记区间 -> 有序/编号词头（读真实交付包）
    investigation.py     约束式全序求解 + 试核定位
    book.py              新书帖号/位置查询 + 互斥散页解释
    handoff.py           排字单（前缀压缩）+ 字序凭单 + 新散页撤销
    pipeline.py          真实包 + 场景 spec -> 报告
    demo.py              collation-demo 端到端（自行发布真实包）
tests/test_delivery.py   20 个测试：覆盖、过期、重组、不可变、规模
tests/test_collation.py  50 个测试：分字/调查/装订/交接/真实包集成/撤销
materials/collation/     真实冻结包 + 四个可复跑场景 spec
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

## 规模与不扩平台

测试覆盖 100 源段 / 5 语言 / ≥500 译文（含大量重叠诱饵），验证每语言精确覆盖、
诱饵从不入选、更正后按缺口隔离、补译后恢复。覆盖是 O(n·k) 区间 DP，
足以支撑题目规模；未引入翻译、语义评分、账号等平台能力。

## 铅字字序调查 + 装订复排（`delivery_system/collation`）

旧词表与新书词条**不是手写文本**，而是从真实 `translation-package/1` 交付块里，
按校印员标出的**精确字符区间**和对应**源段修订**读出的编号词头。因此：

- **分字**：地方字模表含单字与整块连写字模，按**从左到右最长匹配**分字
  （`glyphs.TypeMatrix`）。区间越界、分不干净、旧块误贴到新版本都会在成词表前被
  拒绝（`IntervalOutOfBounds` / `Untokenizable` / `StalePackage` / `UnknownBlock`）。
- **旧词表顺序** = 译块实际出场顺序，块内按区间起点（`marks.build_wordlist`）。
- **比较规则**：首个不同字模定先后；短前缀在长词前；词头相同按稳定词条号
  （数值优先，`2 < 10`）定序（`compare_headwords`）。

### 调查：约束式全序
一本/数本旧表给出若干相邻词头对；每对贡献一条 `字模x < 字模y` 不等式
（首个不同字模处），前缀/同字乱序则是与顺序无关的矛盾，直接报出。
所有相容全序 = 该不等式 DAG 的**线性扩展**，用约束表达、按需枚举（默认上限
20000，超出抛 `TooManyOrders`），不列遍排列。`CollationInvestigation.check(一组字序)`
可试核；矛盾时定位到**具体词头对 + 出处块/段/修订**。

### 装订：整书相容
新书是真实词条 + 每帖容量；给定一个全序即得整书布局（按字序排序、按容量切帖）。
散页线索：`某词条在第F帖第P位` 或 `同帖上甲紧挨乙在前`，且**所有散页同属一书**。
对指定词条，`BookSolver.query` 返回在**同时相容于调查和全部散页**的每本整书中
它确实可能出现的每个 (帖, 位)，每处附一见证全序便于复排；位置唯一则给出
`signed_position` 供签入，否则保留全部真实可能位置。零解释时返回**最小互斥线索
子集**（可两两成立、三者不能同属一书）或调查矛盾。

### 交接（两条路都可复排）
- **调查员→装订组**：`build_slip` 产前缀压缩排字单；先剔除版面相同的字序，
  再按共用前缀紧凑记录各可见书序。外部人员仅凭排字单即可 `reconstruct_book_orders`
  复排，并能 `match_clues_against_slip` 独立对位残页。
- **装订组→调查员**：`certify_precedence` 按某帖位要求的先后关系，只返回**真正可行
  的全序凭单**（或冲突）。局部结论记录其依赖散页；`revoke_on_new_clue` 在新散页
  推翻旧推断时**只撤销被推翻的局部结论**，其余保留。

### 复现要点（对应 brief 示例）
三字模 `a,b,c`，旧表仅 `a<b`：新书 `aa,ab,b,c`（每帖一条）中 `c` 可能在**第 1、3、4 帖，
绝无第 2 帖**；补入旧表 `c,a` 后 `c` **只在第 1 帖**；仅旧表 + 散页 `b` 在第 3 帖则
`c` **只在第 4 帖**。`scenario_4` 则给出三张两两可成立、整体不能同属一书的散页。
全部结果都在 `collation-demo` 与 `materials/collation/` 中可复跑；同字词头按词条号
定序、以及源更正后**旧包仍按原散页还原原书序**亦在 demo 中验证。

规模：每套至多 12 字模 / 60 新词 / 12 散页线索；在约 1.2 万相容全序的极端情形，
求解、查询、排字单、凭单均在~1 秒内完成。


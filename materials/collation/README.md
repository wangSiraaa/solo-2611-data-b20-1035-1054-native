# 铅字字序调查 + 装订复排 场景（`materials/collation`）

这些 JSON 不是手写产品，而是**真实发布包**：`packages/` 下的每个
`PKG_*.json` 都是用 `delivery_system` 的 `SourceCatalog` +
`DeliveryRepository` 实际冻结出来的 `translation-package/1`。场景 spec 里的每个
词头都是这些包里**某一块正文的精确字符区间**，并带它校对时所对的源段修订。

## 内容

```
packages/
  PKG_OLD_AB.json    旧表 a、b（两个单字块）
  PKG_OLD_CA.json    旧表 c、a
  PKG_OLD_ABC.json   旧表 a、b、c
  PKG_OLD_DUP.json   两个同字词头（2、10），示范按稳定词条号定序
  PKG_NEW4.json      新书四词条 aa、ab、b、c（每帖一条）
  PKG_NEWCONF.json   新书六词条 a、b、c、d、e、a（每帖两条）
  PKG_LIGCONF.json   含连写字模 ab 的译本：ab、a、b、c、ab（每帖两条）
scenario_1_single_old_table.json   仅旧表 a<b        -> c 可能在 1、3、4 帖，绝无 2 帖
scenario_2_two_old_tables.json     再补旧表 c<a       -> c 只在 1 帖
scenario_3_loose_page.json         仅旧表 + 散页 b 在第 3 帖 -> c 只在第 4 帖
scenario_4_joint_conflict.json     三张散页两两可成立、三者不能同属一书 -> 互斥线索
scenario_5_ligature.json           同一对性质改用含连写字模 ab 的译本验证：
                                   单页两两成立、全册不能同时成立 + 同字词头定序
```

## 运行

```bash
# 端到端（自行发布真实包，再跑全部场景，含旧修订还原）
python3 -m delivery_system collation-demo

# 用给定 spec 对真实包文件跑一次调查 + 装订
python3 -m delivery_system collation-run \
  --spec materials/collation/scenario_3_loose_page.json \
  --package materials/collation/packages/PKG_OLD_AB.json \
  --package materials/collation/packages/PKG_NEW4.json
```

`scenario_4` 用 `PKG_OLD_ABC.json` + `PKG_NEWCONF.json`：

```bash
python3 -m delivery_system collation-run \
  --spec materials/collation/scenario_4_joint_conflict.json \
  --package materials/collation/packages/PKG_OLD_ABC.json \
  --package materials/collation/packages/PKG_NEWCONF.json
```

`scenario_2` 用 `PKG_OLD_AB.json` + `PKG_OLD_CA.json` + `PKG_NEW4.json`。
`scenario_5_ligature` 用 `PKG_OLD_ABC.json` + `PKG_LIGCONF.json`。

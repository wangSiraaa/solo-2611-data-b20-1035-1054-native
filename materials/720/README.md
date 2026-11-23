# 720 材料

- `inventory.json`：合成封签泥库存。`W1` 4 份、印刷有效期 30；`W2` 1 份、
  印刷有效期 12（用于演示过期未开成）。方法 `seal` 有两个修订，分别需求
  1 份和 2 份封签泥。
- `plan_example.json`：货运处置计划文档样例。点名炉批 `A`，最多陪扣 1 只
  已证清白箱，外柜原扣 4 工时、拆封 1 工时、叶箱单扣 2 工时。

印带读档 JSON 的格式（`imprint-record --imprint`）：

```json
{
  "segments": ["10", "10"],
  "splice_marks": [0, 2],
  "locating_hole": "present",
  "note": "optional"
}
```

- `segments`：每段一个字符串，字符仅 `0` / `1` / `?`，`?` 是仍占一格的磨损格。
- `splice_marks`：压平后的局部邻接序号；不允许落在段间空隙上。
- `locating_hole`：`present` 只能正读；`missing`（定位孔整个脱落）才允许反读。

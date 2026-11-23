# 长条窗花读样与针模校准（合成样例）

- `calibration.json`：一副未拆开的可调针模（左针 0 号、右针 1 号），加一张校样
  “B 有孔”。左针轴可深一层或两层，右针停用。
- `orders.jsonl`：订户订单。每单自带折法（译块）与针位起点，必须用同一副设置判定。
  - `o1` 左折 B 有孔、C 完整 → 暂缓（两种设置都在，只有一层深能交）。
  - `o2` 只把译本换成右折（校准不变）→ 左针先碰 D，任一设置都交不出 → 退单。
  - `o3` 保留左折、C 不限 → 两种设置都交得出 → 准开。
  - `shared-false`：两针共用一轴的反例，拼凑各自有利深度会误放行。

运行：

```bash
python3 -m delivery_system calibrate --calibration materials/strip/calibration.json
python3 -m delivery_system judge-order o1 --cells A,B,C,D --fold L --steps 1 \
    --punch B --intact C --calibration materials/strip/calibration.json
python3 -m delivery_system calibration-demo
```

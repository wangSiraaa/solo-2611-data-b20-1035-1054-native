# 双语纵横字谜 · materials fixture

合成的小写拉丁字母素材，无自然语言推断；词根与词尾均由 `clues.json` 明给。
用于 `xw-*` 命令行与离线交接的复现。两端物理分离：源段目录 / 交付仓 / 谜面册
（`clues.db`）/ 造词仓（`coinage.db`）各持一个 SQLite。

## 一键离线复现

```bash
T=$(mktemp -d)
G="--source-db $T/source.db --delivery-db $T/delivery.db --clue-db $T/clues.db --coinage-db $T/co.db"

python3 -m delivery_system.cli --source-db $T/source.db --delivery-db $T/delivery.db \
  init-materials --source source.json --translations translations.jsonl
python3 -m delivery_system.cli --source-db $T/source.db --delivery-db $T/delivery.db publish XY PK1

python3 -m delivery_system.cli $G xw-clue-add --file clues.json
python3 -m delivery_system.cli $G xw-sheet-add --file sheet_h.json
python3 -m delivery_system.cli $G xw-sheet-add --file sheet_v.json

echo '["H","V"]' > $T/ids.json
# 原盘：ambiguous（ab/bd 或 ac/cd），交叉格不预印
python3 -m delivery_system.cli $G xw-board --clue-ids $T/ids.json --board board.json
# 锁交叉格为 b：unique
python3 -m delivery_system.cli $G xw-board --clue-ids $T/ids.json --board board.json --lock 0:1:b
# 擦掉锁重排：ambiguous 再现
python3 -m delivery_system.cli $G xw-board --clue-ids $T/ids.json --board board.json --erase 0:1
```

## 示例结论

- 原盘可填 `ab/bd` 或 `ac/cd`，两角 `a`、`d` 已定，交叉格不定。
- 撤掉横词 `ac` 后交叉格必为 `b`，纵词必为 `bd`（见 `xw-demo`）。
- 若在盘上把交叉格锁成 `c`（在新造词单已无 `ac` 的情况下）→ `contradictory`，
  并指出该锁字。
- 相对 `run_demo.py --crossword`，本 fixture 走真实多库离线命令行。

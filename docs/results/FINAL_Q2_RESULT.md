# B 题 Q2 最终结果（Round 3）

## 最终 validated solution

最终 official CSV 位于 `outputs/q2/best`，原子复制自 immutable repro：
`outputs/q2/runs/20260816-q2-round3-final-repro`。

| 指标 | 最终值 |
|---|---:|
| Aircraft time | **17,218 min** |
| Passenger time | **254,656 min** |
| Flights | **95** |
| Fuel | **132,473.9 kg** |
| Utilization | **0.9270255505** |
| Served | **4,000 / 4,000** |
| Validator | **PASS, 0 issues** |

内部 metrics 与独立 Validator 完全一致。`outputs/q2/best-95-flight` 与 final repro 的
routes/assignments SHA-256 完全相同。

## 改善

| Control | Aircraft time | Round-3 改善 |
|---|---:|---:|
| Canonical RMP | 19,736 | **2,518 min / 12.7584%** |
| Standard ALNS | 18,906 | **1,688 min / 8.9284%** |
| Round 1 | 17,958 | **740 min / 4.1207%** |
| Round 2 | 17,595 | **377 min / 2.1427%** |

## Winning lineage

最终 primary pipeline 仍是 classical OR：fixed 4-route neighborhood、geometry-ranked
bounded 1–5-stop candidates、SA、adaptive roulette、Shared Solver Core 与 Exact Local
MILP Repair。Round-3 使用 stagnation stop 与 checkpoint/restart，而不是固定 wall-clock。

1. global-best lineage 401：17,595 → 17,409；
2. restart lineage 402：17,409 → 17,231，并在 iteration 124 由普通
   low-utilization 4-route repair 完成 96→95，primary 一次下降 100 min；
3. 95-flight continuation：primary 不变，改善 secondary；
4. low-frequency targeted 5-route：17,231 → 17,230（1 min）；
5. `ml_exploration` run 501 意外得到 17,218；两次 primary gain 均来自 incumbent
   sequences，不把 portfolio/ML 宣称为原因；
6. 最终纯 geometry continuation 保持 17,218，并将 passenger time 降至 254,656。

累计 lineage runtime-to-final-comparator-best 为 2,451.172 s，累计 844 repairs；
winning primary run 501 内部 runtime-to-best 为 91.416 s、18 repairs。所有 bound/gap
仅属于 finite restricted local master，不是 Q2 global gap。

## 组件结论

- 强制 source absorption 的 22 个诊断窗口未找到 95-flight incumbent；正式 flush 的
  前五个 4/5/6-route窗口也无 incumbent。第二个 6-route 窗口成本病态，故 generic
  6-route **REJECT**。
- 真正 96→95 来自普通 low-utilization 4-route exact repair：4 routes → 3，local
  master 806 assignments，restricted gap 0，primary gain 100。
- targeted 5-route：弱 ADOPT，仅贡献 1 min，无 flight elimination。
- path relinking V2：4 attempts、145.281 s、0 accepted，当前 basin REJECT。
- cross-exchange：Round-3 11 uses、0 primary gain；不进入最简 finalist。
- LAND-heavy：lineage 401/402 合计贡献 60 min，保留。
- cross-airport regrouping：legality 由 Shared Core 覆盖，但无可隔离 direct gain。
- promising local-master queue：强制 absorption run 无 incumbent/gap entry，0 deep retry；
  未提供新 best。
- repeated visits、heuristic Column Generation、Full Branch-and-Price 继续 REJECT。

## 正式文件

- `outputs/q2/best/q2-routes.csv`
- `outputs/q2/best/q2-assignments.csv`
- `outputs/q2/best/q2-validator.json`
- `outputs/q2/best-95-flight/`
- `Q2_ROUND3_FINAL_COMPARISON.csv`
- `ROUND3_CONTROL_MANIFEST.json`
- `outputs/q2/round3-experiment-summary.json`
- `outputs/q2/ml-data-round3/`

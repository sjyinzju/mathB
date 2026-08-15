# B 题 Q2 最终结果说明（Round 2）

## 1. 最终结果

最终 official CSV 位于 `outputs/q2/best`，原子复制自 immutable run：

`outputs/q2/runs/20260815-q2-round2-final-repro`

该 repro run 从真实 winning search run
`20260815-q2-round2-extended-round1-control-s30` 载入，0 iteration 逐字节复现并独立验证；
完整 180 秒 search log 保留在 source run，`best` 不复制其 271 MB candidate log。

| 项目 | 最终值 |
|---|---:|
| 已安排人员 | **4,000 / 4,000** |
| 飞机总使用时间 | **17,595 min** |
| 人员总在途时间 | **259,487 min** |
| 总架次数 | **96** |
| 总燃油消耗 | **135,954.1 kg** |
| 座位利用率 | **0.9102123546** |
| 独立 Validator | **PASS，0 issues** |

内部 metrics 与 Validator 五项指标完全一致。Round-1 17,958 immutable run 与
canonical 19,736 baseline 均保持不变。

## 2. 最终 winning pipeline

最终解不是 ML 模型的产物，本轮没有训练任何模型。winning pipeline 为：

1. Round-1 fixed 4-route Adaptive ALNS + SA + geometry-ranked bounded 1–5-stop
   candidates + Exact Local MILP Repair；
2. extended multi-restart、quality-constrained elite exact recombination、global-best
   restart 与 cross-exchange 产生多个 validated elite；
3. 一个独立 `ml_logging` portfolio run 意外产生 17,656 validated elite；
4. 从该 elite 用 Round-1 geometry control 配置继续 180 秒 extended search；
5. 第 26 次 low-utilization 4-route exact repair 将 4 条架次重排为 3 条，飞机时间
   一次下降 61 分钟，得到最终 17,595 / 96 flights。

最后一步使用 24 条 bounded candidate sequences，local master 有 1,221 个 compatible
assignments，选入两条新 3–4-stop columns。该次 restricted local master status 为 time
limit，restricted gap 为 0.058651；这只描述有限局部候选池，不代表 Q2 全局 gap。

## 3. 相对改善

| 对照 | Aircraft time | 改善 | Passenger time | Flights | Fuel | Utilization |
|---|---:|---:|---:|---:|---:|---:|
| Canonical RMP | 19,736 | **-2,141 (-10.85%)** | -11,247 | -11 | -16,956.3 | +0.0919141 |
| Standard ALNS control | 18,906 | **-1,311 (-6.93%)** | -6,821 | -7 | -10,488.4 | +0.0565089 |
| Round-1 final | 17,958 | **-363 (-2.02%)** | -4,101 | -1 | -2,121.1 | +0.0127492 |

飞机时间始终为第一目标；96 flights 是搜索产生的结构性结果，不是替代 primary 的新目标。

## 4. Round-2 公平实验

同一 17,693 起点、seed 11/12/13、90 秒目标、同 Shared Solver Core：

| 配置 | 三种子 aircraft time | Best | Median | Mean | Std |
|---|---|---:|---:|---:|---:|
| Round-1 geometry control | 17,693 / 17,693 / 17,688 | 17,688 | 17,693 | 17,691.33 | 2.36 |
| Geometry+context portfolio | 17,679 / 17,693 / 17,693 | 17,679 | 17,693 | 17,688.33 | 6.60 |
| Round-2 finalist | 17,671 / 17,671 / 17,667 | **17,667** | **17,671** | **17,669.67** | **1.89** |

Round-2 finalist 在 best/median 上分别比 control 改善 21/22 分钟，3/3 胜出。它由
geometry control、cross-exchange 与停滞触发 5-route 组成。portfolio 虽有更好单点，
但 context/exploration unique columns 没有 new-best 贡献，因此不进入默认 finalist。

## 5. Extended search

同一 17,656 起点、seed 30、180 秒目标：

- Round-2 finalist：17,634 / 97 flights；
- Round-1 algorithm control continuation：**17,595 / 96 flights**。

最终 control 在约 110 秒累计 repair 时间出现 61 分钟 route elimination；之后 19 次
repair、约 72 秒没有 primary 改善，因此停止，没有无限续跑。

## 6. 组件结论

- Extended baseline：**ADOPT**，17,958 自然刷新为 17,853/17,854。
- Stronger elite recombination：**ADOPT**，17,853 → 17,837。
- Global-best elite restart：**ADOPT**，17,837 → 17,798。
- Diversity-heavy partner：**REJECT**，成本病态且不如质量受限 partner。
- Lightweight path/difference relinking：**ADOPT 为小规模 exact difference steps**；
  objective-near run 4 steps 接受 2 steps，不扩展为通用框架。
- Targeted 5-route：**弱 ADOPT**；有一次 accepted secondary new-best，但没有直接
  primary gain 或 flight delta，仅作为停滞 intensification。
- Dedicated flight-elimination guidance：**不作默认**；8 个 5-route windows 没有删架次，
  但贡献 9 分钟 primary improvement。最终 97→96 由普通 low-utilization 4-route 完成。
- Fix-and-optimize：**REJECT**，90 秒无 primary 改善。
- Local Branching：**feasibility REJECT**；需要改 aggregated master 的稳定 incumbent
  binary identity，超出 bounded experiment，不另写第二套 MILP。
- Cross-exchange：**ADOPT**，同起点下降 47 分钟。
- Geometry+context portfolio：**OPTIONAL / logging only**，公平 best 有信号但无
  context-only useful column 证据，默认 REJECT。
- Repeated visits：**继续 REJECT**；96 flights 已由 distinct-service model 获得，
  没有 accepted repair 需要重复 occurrence。
- Heuristic Column Generation、Branch-and-Price、UCB 与 Deep Learning：继续 REJECT。

## 7. ML 数据结论

本轮只建立数据基础，不训练模型。`outputs/q2/ml-data` 含 136,597 candidate rows，
其中 12,467 exact-evaluated、12,343 true negatives、72 positives、52 invalid、
124,130 censored。6 个独立 runs 按 run 分组，train/validation/test positives 为
32/24/16，candidate ID 无重复、无 run leakage。

Schema 与 split 基础可用，但 useful positives 仍主要集中在 incumbent sequences，
exploration/context-only 候选没有形成足够 useful positives。因此当前 ML gate 为
**NOT_READY**，不能把 censored 当 negative，也不应立即训练 LR/LightGBM。

## 8. 正式文件

- `outputs/q2/best/q2-routes.csv`
- `outputs/q2/best/q2-assignments.csv`
- `outputs/q2/best/q2-validator.json`
- `Q2_ROUND2_FINAL_COMPARISON.csv`
- `ROUND2_CONTROL_MANIFEST.json`
- `outputs/q2/round2-experiment-summary.json`
- `outputs/q2/ml-data/dataset_diagnostics.json`
- `ML_READINESS.md`
- `NEXT_STAGE_RECOMMENDATION.md`

# Q2 ML Readiness — Round 3

## Decision: READY

本轮只采集/治理数据，没有训练 Logistic Regression、LightGBM、Random Forest 或任何神经网络。

## Dataset V2

目录：`outputs/q2/ml-data-round3/`，schema version 2。

| 统计 | 数量 |
|---|---:|
| Candidate rows | 663,504 |
| Exact evaluated | 309,785 |
| Positives | 1,863 |
| Novel / non-incumbent positives | **89** |
| True negatives | 306,904 |
| Censored | 353,719 |
| Invalid | 1,018 |
| Runs | 11 |
| Parent-chain lineage groups | **3** |

Parent/child runs 被归并到同一 lineage root。train / validation / test positive 数为
495 / 1,026 / 342；novel positive 数为 **26 / 51 / 12**。三个 split 均包含 novel
positives，且无 run/lineage leakage、0 duplicate candidate IDs。

## Coverage

- geometry top/mid/low exact coverage：107,231 / 11,370 / 6,800；
- geometry top/mid/low positives：80 / 6 / 3；
- candidate-source positives：INCUMBENT 1,774，GEOMETRY_TOP 66，GEOMETRY_MID 2，
  EXPLORATION_RANDOM 20，CROSS_EXCHANGE 1；
- novel 3/4/5-stop positives：34 / 19 / 6（另有 30 个 novel 2-stop）；
- positives 跨 low-utilization、high-cost、shared-flow、LAND-heavy、cross-exchange 与
  flight-elimination operators；
- absorption/context-only/path-relink positives 均为 0，因此这些 source 不应获得高
  exploration 配额。

CENSORED 始终不作为 negative；TRUE_NEGATIVE 仅来自 exact-evaluated candidates。
features 在 exact/MILP/acceptance outcome 前生成，schema 明确排除 outcome fields。

最可信 target：
`P(MILP-selected AND accepted useful repair | exact-evaluated candidate, context)`。

下一阶段先训练可解释 Logistic Regression baseline，再做 LightGBM ranking 公平 A/B；
Random Forest optional。仍不考虑 GNN、Transformer、Deep RL。

# Q2 ML Readiness

## Decision: NOT_READY

Round-2 已完成可复现的数据与接口基础，但当前数据还不足以公平训练 candidate/repair
ranker。本轮没有训练 Logistic Regression、LightGBM、Random Forest 或其他模型。

## Dataset

目录：`outputs/q2/ml-data/`

| 统计 | 数量 |
|---|---:|
| Candidate rows | 136,597 |
| Exact-evaluated | 12,467 |
| True negatives | 12,343 |
| Positives | 72 |
| Invalid | 52 |
| Censored / not evaluated | 124,130 |
| Accepted repairs | 60 |
| New-best repairs | 11 |
| Independent runs | 6 |

已生成：

- `candidate_events.csv`
- `repair_events.csv`
- `run_manifest.csv`
- `split_manifest.csv`
- `feature_schema.json`
- `label_schema.json`
- `dataset_diagnostics.json`

## Correctness properties

- candidate ID 由 run/seed/iteration/destroy/neighborhood/sequence/variant/airport/type
  的稳定 key 生成，本数据集中 0 duplicates；
- CENSORED 与 TRUE_NEGATIVE 严格分离；
- INVALID 单独标注；
- feature schema 排除 exact/MILP/acceptance/outcome 字段；
- split 按 run 分组，不做 candidate-level random split；
- train/validation/test positive 数分别为 32/24/16；
- 46 个 feature/search-context 字段，核心 geometry/flow/capacity/route/search 字段完整；
- context composite 对 incumbent rows 为可预期缺失，targeted trigger 仅在触发时非空。

## Why NOT_READY

72 个 useful positives 虽跨多个 run/split，但几乎都来自 incumbent service sequences。
三个 exploration logging runs 中只有 1 个 exploration variant 被 local MILP 选中，且
没有成为 useful positive；context-only candidates 也没有贡献 accepted new-best。

因此当前数据足以学习“incumbent variant 在 exact repair 中是否会被复用”，但不足以
学习真正需要的“未见候选是否值得进入 exact evaluation”。若现在训练，模型很可能
学习 incumbent/selection policy，而不是候选的真实改进价值。

## Most credible target

当前最可信但仍需更多覆盖的 target：

`P(MILP-selected AND accepted useful repair | exact-evaluated candidate, context)`

即 label `POSITIVE`。训练时只能在 exact-evaluated 样本上使用 TRUE_NEGATIVE；不得把
CENSORED 当 negative。次选 target 是 `expected primary gain / evaluation cost`，但当前
positive 数量和 gain 分布不足。

## Readiness unlock condition

继续少量 run-grouped exploration logging，要求：

- 多个独立 runs 出现非-incumbent、context/exploration candidate positives；
- positives 不集中于单一初始解或单一 operator；
- train/validation/test 均包含这类 novel positives；
- exact-evaluated coverage 足以比较 geometry rank bins 与 exploration rank bins；
- 特征仍在 outcome 前生成。

达到后，先做 Logistic Regression 可解释基线，再做 LightGBM/ranking 公平 A/B；Random
Forest optional。继续不考虑 GNN、Transformer、Deep RL。

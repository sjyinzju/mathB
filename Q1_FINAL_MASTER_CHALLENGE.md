# Q1 Final Master Challenge

## 结论

状态为 **PROVEN_RESTRICTED_INFEASIBLE**。在当前完整收集到的 restricted allocated-route-pattern universe 中，加入硬约束

\[
\sum_r c_r x_r \le 14{,}729
\]

后，HiGHS 1.15.1 完整搜索并严格返回 `Infeasible`。因此 **14,730 是当前 restricted pool 的整数最优 primary value**。这不是 Q1 全局最优性证明；未生成列仍可能改变结论。

## Reproduction 与 MIP start Gate

当前 pool 含 97 个去重 source solutions、391 个 semantic physical routes、1,003 个 canonical allocated-pattern columns、104 个 OD demand equalities、2,517 个非零 coverage coefficients。

Frozen `14,730 / 121,363 / 89 / 118,624.4 / 1600` 解的 89 个 sorties 全部精确映射到 84 个非零 pattern variables：

- missing patterns：0；
- demand equality 最大残差：0；
- primary：14,730；
- passenger：121,363；
- flights：89；
- fuel：118,624.4 kg；
- independent Validator：VALID，0 issues。

Direct HiGHS API 的 `setSolution` 返回 `HighsStatus.kOk`，且 backend 日志明确报告 `MIP start solution is feasible, objective value is 14730`。因此这里传入的是完整合法 incumbent vector，不是单独的 objective value。

14,730 vector 对 base master 可行，但对新增的 `<=14,729` hard row 必然有 1 分钟 violation。严格模型中它只能作为 backend repair hint，不能称为 strict incumbent；日志也明确报告该 row infeasibility。

## Symmetry Audit

当前 Master 已在设计层面消除了主要的无意义排列对称性：

1. individual person IDs 不构成变量；同一 OD class 精确聚合为 count；
2. allocation input ordering 被排序、合并为 canonical OD-count tuple；
3. physical route identity 包含 base、type、ordered physical stops、refuel/service flags 与 service semantics；
4. 只删除完全相同的 `(semantic route, canonical allocation)` 列。

审计结果：retained exact duplicate columns = 0，route-ID semantic collisions = 0，具有相同全部 Master coefficients 但不同 route semantics 的组数也为 0。因此本阶段没有虚构额外 symmetry reduction。尤其没有按 passenger ID、启发式 route score、nearest-K 或 source frequency 删列。

## Strict Challenge 证据

| 项目 | 结果 |
|---|---:|
| Backend | direct `highspy` 1.15.1 |
| Hard UB | 14,729 min |
| Solver status | Infeasible |
| Outcome | PROVEN_RESTRICTED_INFEASIBLE |
| Nodes | 358,676 |
| LP iterations | 20,134,669 |
| Elapsed | 2,041.5 s |
| Fixed wall limit | none |
| Dynamic stall limit | 300 s without node/bound/incumbent change |
| Stall stop triggered | no |
| Strict incumbent found | no |

求解没有在旧的 600 秒阈值停止；超过 600 秒后仍持续推进，最终探索 100% tree 并返回 infeasible。

## Scope Boundary

本结论只证明：

> 当前 1,003-column restricted allocated-pattern master 中不存在 aircraft time <=14,729 的整数可行解。

它不证明完整合法 Q1 column universe 中不存在此类解，也不把 restricted LP `14,208.636981` 当作 global lower bound。Global claim 仍需 exact pricing 覆盖完整合法列空间，并在必要时完成 Branch-and-Price。

机器可读证据位于 `outputs/q1/exact/final-master-challenge/20260816-highspy-strict-14729/`。

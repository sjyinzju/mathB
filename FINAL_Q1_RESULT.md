# Q1 最终 OR / Matheuristic 结果

> Exact certification update：当前 14,730 仍是 validated UB。现已严格证明它是最终 fully-priced root 1,041-column master 的整数最优，并建立 full-space/tree global lower bound 14,090.327485380118（ceil 14,091）。Branch-aware pricing 已认证，root、两个 depth-1 children 与一个 depth-2 node 均 fully priced；当前仍有 5 个 open unpriced nodes。因此全局状态为 **BRANCH_AND_PRICE_INCOMPLETE**；不得称 14,730 为 global optimum。

## 最终结论

最终算法为 **Master-recombined + Standard ALNS education**：先用跨轨迹 Elite Route Pool 的 exact allocated-route-pattern master 重组，再用 Standard ALNS 对 14,730 master 解做 education。在所有独立验证通过的候选中按正式词典序选择后，最终结果为：

| 指标 | 最终值 |
|---|---:|
| 总飞机使用时间 | **14,730 min** |
| 人员总在途时间 | **121,363 min** |
| 总架次 | **89** |
| 总燃油 | **118,624.4 kg** |
| 座位利用率 | **0.4942969225** |
| 安排人数 | **1600/1600** |
| Validator | **VALID（0 issues）** |

最终 CSV 的 SHA-256：`q1-routes.csv = 148142c883094d8edd6c40b57ed8dfe7d205aeca86ee7c9d0ec08bc0317faf78`，`q1-assignments.csv = 3875c5b279071cdebce7d9038f0d687d29d41349cf11a0e4c5e924a79f40f977`。

## 改善幅度

| 对照 | 时间 | 改善 | 改善率 |
|---|---:|---:|---:|
| Classical VND | 15,371 | 641 min | 4.17% |
| Fair Standard ALNS | 15,118 | 388 min | 2.57% |
| Old absolute best | 15,052 | 322 min | 2.14% |
| Frozen Q1 control | 14,770 | **40 min** | **0.27%** |

## 关键研究问题

1. **Route Pool 有效。** Round 1 直接把 14,770 降至 14,743；后续搜索反馈产生的新 route 又帮助 master 降至 14,730。
2. **Exact Master 有效。** Frozen 14,770 control 被 exact reconstruction 完整重放，1600 人覆盖、指标和 Validator 均通过；这证明 master 语义正确。
3. **Master → ALNS feedback 有效。** Standard ALNS 从 14,730 master warm start 保持 primary 14,730，并将 passenger time 从 122,494 降至 121,363。
4. **89 → 88 成功。** 找到 VALID 的 14,732 / 88-flight 解；它比 frozen control 更好，但 primary 比最终 14,730 差 2 分钟，因此不取代最终解。
5. **Elite recombination 部分有效。** 整体 elite population 与 exact master 的跨轨迹重组有效；单独 A2×R3 difference-region child 为 14,770，没有刷新。
6. **Path relinking 无效。** A2 与 R3 只有 3 个差异步，本次没有形成有效 path progress，最好仍为 14,770。
7. **Cross-exchange 仅改善 secondary。** 在 14,732 / 88-flight 解上把 passenger time 从 123,171 降至 123,113，未改善 primary。
8. **High-impact / block removal 无 primary 收益。** 受控 exact neighborhoods 没有超过 incumbent。
9. **Targeted 6–10-route neighborhoods 无 primary 收益。** 19 次 targeted attempts 中仅 cross-exchange 改善 secondary；10-route 最好局部 delta 为 +28，继续扩大不划算。
10. **Restricted LP bound 为 14,208.636981。** 对应最终成功 pool 的 integer master 14,730，restricted-pool gap 为 **3.6693%**。这只是当前有限 route pool 的 LP gap，**不是 Q1 全局最优性 gap**。
11. **Heuristic pricing 未进入。** Integer master 仍通过已有列组合出新 best，且 LP 分数结构显示组合/整数性仍是主导瓶颈；没有证据证明 missing columns 已成为唯一主因，因此新增 useful pricing routes 为 0。
12. **HGS-inspired population 有效但作用来自轻量结构。** Quality-first elite population、common-route inheritance、exact difference recombination 和 education 的闭环有效；没有必要重写完整 HGS。
13. **CP-SAT 无追加价值。** OR-Tools 不是项目依赖，现有 MILP exact repair 已稳定可行；当前瓶颈也不是 repair backend，故拒绝引入第二套复杂后端。
14. **Regret / Beam 无价值。** 同一 6-route、110-passenger neighborhood 上，MILP 局部目标 1,586；Regret-3 和 Beam 最好均为 1,615，Regret-2/4 更差。
15. **Mild reheating 无价值。** 固定 seed 实验与 control 的最终指标及产物一致，没有 basin breakthrough。
16. **Relatedness 的最终角色是 diversification / soft guidance / route-pool contributor。** 它帮助提供不同 basin 和候选 routes，但最终 winner 的 education 仍是 Standard ALNS。
17. **ML 数据集 NOT_READY。** 当前只有 19 个 evaluated candidate events、1 个 positive、1 个 lineage，不足以进行可靠的按 run/lineage 分组训练和评估；本阶段未训练任何模型。

## 最终算法判断

当前证据对应 **CASE A，但只剩极窄的 classical intensification 空间**。最值得继续的唯一方向是：给 route-pattern master 加对称性消除，并换用支持 incumbent/MIP start 的求解后端做一次严格上界 14,729 的聚焦搜索。SciPy `milp` 当前不支持 warm start，Round 5 在 600 秒内没有 incumbent；这不是 infeasibility proof，也不能宣称 14,730 最优。

除上述一次聚焦 master 实验外，建议冻结 Q1。暂不进入 LR → LightGBM，不重启 clustering、SA grid 或完整 Branch-and-Price。

## 正式产物

- `outputs/q1/final/q1-routes.csv`
- `outputs/q1/final/q1-assignments.csv`
- `outputs/q1/final/metrics.json`
- `outputs/q1/final/validator.json`
- `outputs/q1/final/winning_config.json`
- `outputs/q1/final/method_metadata.json`

详细证据见 `Q1_FINAL_OR_COMPARISON.csv`、`Q1_ROUTE_POOL_REPORT.md`、`Q1_MASTER_LP_DIAGNOSTICS.md`、`Q1_EXACT_REPAIR_AB.md`、`STAGE_Q1_FINAL_OR_HANDOFF.md` 和 `NEXT_Q1_STAGE_RECOMMENDATION.md`。

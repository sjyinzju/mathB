# Q1 Exact Pricing Report

Pricing oracle 使用 direct HiGHS 1.15.1 的 position-indexed MILP。每个 base×type 模型显式选择至多 5 个 offshore positions、相邻 arcs、return position、visited OR、refuel flags、arrival/departure fuel 与 integer OD allocations。Fuel big-M 仅解除 inactive position 的递推；active positions 使用等式燃油递推、reserve lower bound、refuel-to-full 与 return reserve。

验证 Gate：8 个 pricing tests 全部通过，包括 tiny complete enumeration、refuel、repeated sequences、fixed-airport exclusion、LAND、5 个 randomized exhaustive cases、tolerance，以及有限 RMP reduced-cost cross-check。Shared evaluator 对每个 oracle winner 复算 route minutes，必须与 MILP objective decomposition 一致。

初始真实九子问题全部达到 `Optimal`：

| subproblem | min rc |
|---|---:|
| A01×T1 | 27.263158 |
| A01×T2 | -1.460317 |
| A01×T3 | -10.687500 |
| A02×T1 | 18.619883 |
| A02×T2 | -10.801170 |
| A02×T3 | -13.213450 |
| A03×T1 | 43.263158 |
| A03×T2 | -8.342105 |
| A03×T3 | -18.467105 |

因此初始 pool 不完整。Full CG 运行 16 轮、调用 144 个真实 full-instance exact pricing problems，全部完成为 `Optimal`。共生成 38 个负列；最后一轮九项 minimum rc 均不小于 `-1e-7`，其中若干为数值零。

Oracle status：**CERTIFIED EXACT for the defined complete Q1 primary column universe**。机器结果位于 `outputs/q1/exact/pricing-tests/20260816-initial-nine/` 与 `outputs/q1/exact/column-generation/20260816-fullspace-cg/`。

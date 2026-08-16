# Q1 Branch Pricing Validation

状态：**CERTIFIED EXACT** for the existing complete Q1 primary column definition and aggregate directed-arc branching family。

新增 `tests/test_q1_branch_pricing.py` 含 13 个 tests：

1. finite complete universe 对 `<=` branch 的 reduced cost cross-check；
2. finite complete universe 对 canonicalized `>=` branch 的 cross-check；
3. two ancestor rows；
4. three ancestor rows；
5–9. five randomized branch rows/duals vs brute force；
10. repeated directed arc traversal count = 2，而非 presence = 1；
11. node RMP left/right canonical dual sign 与 retained-column reduced costs；
12. Phase-I `route_cost_multiplier=0` pricing vs complete enumeration。
13. adjacent repeated visit 形成的 self-transition arc 仍可 branch，coefficient 按真实次数计算。

这些测试显式枚举 landing length、ordered nodes、refuel flags 与 exact integer allocations，并用 shared evaluator 排除物理非法路线。Oracle winner 与 brute-force minimum reduced cost 在 `1e-7` 内一致。

原 `tests/test_q1_pricing.py` 9 项继续通过；combined pricing tests = 22/22。全仓回归 = 81/81，exit code 0。

真实 full-instance Gate：

| Node | Ancestors | Pricing rounds | Calls | Generated | Final min rc | 9/9 Optimal |
|---|---:|---:|---:|---:|---:|---|
| N1-L | 1 | 2 | 18 | 1 | 0 | yes |
| N1-R | 1 | 3 | 27 | 4 | 0 | yes |
| N2-LL | 2 | 2 | 18 | 1 | 0 | yes |

因此 branch dual sign、multiple ancestors、future-column coefficients 与 repeated traversal semantics 均已通过 finite-universe 和真实 full-instance evidence。

## Fast Exact Pricing Oracle Validation（2026-08-16）

branch-aware pricing 的 DP 实现 `src/solver/q1_fast_pricing.py` 通过同族验证并升级为 runtime 主 oracle（MILP 保留为永久参考与 fallback）：

1. `tests/test_q1_fast_pricing.py` 16/16：finite complete universe（含参数化 branch rows）vs brute force、tiny universe vs HiGHS MILP、randomized duals vs brute force、dominance/bound pruning engages-and-stays-exact、seed 等于最优时仍物化 winner、repeated visits 不重复计数、signature 截断无损、no-demand certificate、validator 一致性、Phase-I multiplier 语义。
2. `_pareto_filter` 三维支配批量过滤：400 次随机属性试验 vs O(n²) 暴力非支配集，全部一致。
3. Gate 8.2 real-duals cross-check（`scripts/22_certify_fast_pricing.py`）：root-iter015 / N1-L-iter001 / N1-R-iter002 / N2-LL-iter001 四组真实 duals × 9 子问题 = 36/36 与存档 MILP 结果在 `1e-6` 内一致，failures = 0；总墙钟 fast 1,798.6 s vs MILP 5,425.2 s（3.0x）。
4. 全仓回归 97/97，exit code 0。

`FAST_PRICING_STATUS = CERTIFIED_EXACT`；完整证明（三维支配、次可加奖励界、生成期剪枝语义）见 `Q1_FAST_PRICING_REPORT.md`。checkpoint 恢复后新 fully-priced 的递归节点全部由 fast oracle 完成（每节点最后一轮 9/9 Optimal），无任何 MILP fallback 触发。

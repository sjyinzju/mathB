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

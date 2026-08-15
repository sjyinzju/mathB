# Q1 Restricted Master / LP Diagnostics

## Formulation 与 reproduction gate

采用与现有 representation 一致的 **A：exact allocated-route-pattern set partitioning**。变量选择完整合法 route/allocation pattern；约束覆盖 1600 名乘客需求并保持 aircraft/base/capacity/physical route semantics。Primary 固定后依次优化 passenger time、flights 和 fuel，secondary 不能换取更差 primary。

Control-only model 有 90 variables、104 constraints、188 nonzeros，在 0.01 秒量级精确重构 14,770 / 120,845 / 89 / 118,640.1 / VALID，primary proven optimal within that control-only model。Reproduction gate **PASS**。

## 最终成功 restricted master

| 指标 | 数值 |
|---|---:|
| Integer incumbent | **14,730** |
| Restricted LP objective | **14,208.636981** |
| Restricted-pool LP gap | **3.6693387%** |
| MIP best dual bound | 14,705 |
| Solver-reported MIP gap | 0.1697217% |
| Variables | 996 |
| Constraints | 104 |
| Matrix nonzeros | 2,497 |
| Solve elapsed | 690.08 s |

定义使用 `(14730 - 14208.636981) / 14208.636981`。这里的 LP objective 和 gap **只属于当前有限 route pool**，不是 Q1 global lower bound / global optimality gap。MIP dual 14,705 同样只对本次 restricted integer model 和求解状态有效。

Round 5 用 strict primary upper bound 14,729 运行 600 秒，没有找到 MIP incumbent；这既不是 restricted model infeasibility proof，更不是 Q1 最优性证明。

## Dual pressure

较高 demand dual（分钟/覆盖单位）包括：

| Demand | Dual |
|---|---:|
| A02→F028 | 28.4737 |
| A02→F030 | 19.7895 |
| LAND→F049 | 17.3567 |
| A02→F035 | 15.5487 |
| A02→F022 | 14.9839 |
| A03 / LAND→F050 | 14.5263 |
| LAND→F037 | 14.0960 |

这些 dual 指向 F028、F030、F049、F035、F022、F050、F037 周边的机场/分配组合压力。它们是 targeted destroy 或未来 route generation 的证据，不应被解释成全局 passenger shadow prices。

LP 中出现大量单设施与双设施 route 的分数 multiplicity，例如 F020 route 1.5789、F002 route 1.1053，以及 F001/F029/F046/F034/F041 等接近 1 的分数列；同时同一 physical route 的不同 allocated patterns 分担需求。这说明当前差距主要包含 set-partitioning integrality、allocation pattern 组合和对称性，不是一个明显的单列缺失现象。

## 瓶颈与 pricing 决策

观察轨迹：Round 1 master 14,743，flight-cap master 14,732，search routes 回流后 Round 4 master 14,730。即 master 持续利用已有/新收集列组合出新 best，符合用户定义的 **CASE 3**；restricted LP 与 integer incumbent 又有 3.67% 差距，表明 pool 内组合/整数性仍未榨干。

因此：

- heuristic pricing：**NOT ENTERED**；新增 useful pricing routes = 0。
- exact column generation：未声称、未实现。
- Full Branch-and-Price：当前 **REJECT / not justified**。缺少“heuristic pricing 持续产生高价值负 reduced-cost routes”和“pricing subproblem 可清晰精确求解”两项前提。
- 最优先的 narrow follow-up：master symmetry breaking + 支持 incumbent/MIP start 的后端，以 14,730 warm start 聚焦验证 `<14,730`。

结论不能写成“missing routes 不重要”；只能写成目前证据不足以把 missing columns 判断为主要瓶颈。

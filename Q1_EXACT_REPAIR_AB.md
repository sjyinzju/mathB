# Q1 Exact / Heuristic Repair A/B

## 受控 neighborhood

在同一个 88-flight incumbent 上移除 6 条 routes、110 名 passengers，候选 route variants 为 201。MILP exact repair 是 control；Regret-k 和 bounded Beam 使用相同候选与 shared evaluator。

| 方法 | 局部时间 | 局部 routes | 用时 | 判断 |
|---|---:|---:|---:|---|
| MILP exact repair | **1,586** | 6 | 10.031 s | CONTROL |
| Regret-2 | 1,799 | 7 | 0.007 s | REJECT |
| Regret-3 | 1,615 | 6 | 0.005 s | REJECT |
| Regret-4 | 1,881 | 7 | 0.008 s | REJECT |
| Beam width 4 depth 2 + Regret-3 | 1,615 | 6 | 0.021 s | REJECT |

Regret-3/Beam 虽快，但完整解 primary 为 14,761，不能改善 14,732 neighborhood start，更不能改善 14,730 final。Beam 没有超过 Regret-3，额外 lookahead 不产生新质量。

## CP-SAT decision

OR-Tools 不在项目依赖中；现有 shared MILP 在该 neighborhood 稳定得到更优 repair，当前主要瓶颈是 restricted master 的组合/整数性而不是局部 repair backend。因此 CP-SAT prototype 决策为 **REJECT**，避免维护两套复杂 legality/backend。这里没有伪造 CP-SAT 数值 A/B。

## Acceptance experiments

Stagnation-triggered mild reheating 在固定 seed 21 上得到与 current acceptance 完全相同的 14,730 / 121,363 / 89 产物，没有形成新 basin，**REJECT**。不做 slow-cooling 或 SA temperature 大网格。

## 结论

MILP exact repair 继续作为 control。Regret-3 可作为极低成本 warm-start generator 保留在研究代码中，但没有成为最终算法组件的质量证据；Regret-2/4、Beam 和 reheating 均不晋升。

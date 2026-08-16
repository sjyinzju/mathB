# Q1 Full-Space LP Certificate

状态：**FULLSPACE_LP_CERTIFIED**。

| 项目 | 值 |
|---|---:|
| Initial columns | 1,003 |
| Exact generated columns | 38 |
| Final columns | 1,041 |
| Pricing iterations | 16 |
| Full-space LP lower bound | **14,090.32748538012** |
| ceil(LB) | **14,091** |
| Validated UB | **14,730** |
| Rigorous gap `(UB-LB)/UB` | **4.3426512%** |
| Pricing tolerance | 1e-7 |

初始标准 RMP（无 artificial fractional caps）为 14,199.521097474075。Exact CG 轨迹依次降至 14,165.443713、14,135.859916、14,122.462879，最终为 14,090.327485。最后一轮九个 base×type pricing MILPs 均全局 `Optimal`，negative subproblems = 0，minimum rc = 0 within tolerance。

所以 final RMP dual 对完整合法 column universe 可行，`14,090.32748538012 <= OPT_Q1` 是严格 global LP lower bound。由于 `ceil(LB)=14,091 < 14,730`，LP relaxation 不能关闭整数 gap，不能声明 global optimum。

证据目录：`outputs/q1/exact/column-generation/20260816-fullspace-cg/`。每轮保存 RMP dual、九个 pricing result、summary 与 generated-column checkpoint。

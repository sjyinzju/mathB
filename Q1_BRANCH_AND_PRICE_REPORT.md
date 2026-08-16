# Q1 Branch-and-Price Report

状态：**BRANCH_AND_PRICE_INCOMPLETE**。

Full-space root LP 已完全定价，bound = 14,090.32748538012，含 67 个 fractional route variables。选定 directed physical arc `A02→F024`，aggregate usage = 3.52046783625731。合法整数 disjunction：左分支 usage ≤ 3，右分支 usage ≥ 4。

该 branching 对所有未生成列都有定义：column coefficient 是 ordered physical route 中该 directed arc 的 traversal count（含 repeated traversals）。两分支覆盖所有整数 usage，并排除当前 fractional root。Node-specific pricing 必须把 branch-row dual 乘以该 arc count 加入 reduced cost；这样仍是 position-indexed exact MILP 的线性 arc cost，保持 tractable/exact。

当前 checkpoint：processed nodes = 1（fully priced root），open nodes = 2，global LB = 14,090.32748538012，global UB = 14,730，rigorous gap = 4.3426512%。两个 child 均为 `OPEN_UNPRICED`；没有把 inherited root LB 当作 child fully-priced bound，也没有 fathom。

未完成原因是 child RMP branch rows、branch dual injection、recursive node pricing/queue/checkpoint engine 尚未实现。机器 checkpoint 位于 `outputs/q1/exact/branch-and-price/20260816-root-initialization/checkpoint.json`。

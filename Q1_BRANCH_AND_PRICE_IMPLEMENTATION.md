# Q1 Branch-and-Price Implementation

## Node and branch state

`BranchNode` stores `node_id`、`parent_id`、`depth`、完整 `branch_history` 与 `inherited_lb`。`ArcBranchRow` 定义 `(directed arc, sense, integer rhs)`，并从每个既有或未来 column 的完整 ordered stops 计算 coefficient。Repeated traversal 按次数计数，不折叠为 binary presence。

Canonical form 为 `s_h sum_r b_hr x_r <= s_h k_h`，左支 `<=` 使用 `s_h=1`，右支 `>=` 使用 `s_h=-1`。两个 children 覆盖所有 integer usage 并排除当前 fractional usage。

## Phase-II RMP and pricing

Demand rows 保持 exact equalities。Canonical branch rows 作为 `A_ub x <= b_ub`；SciPy/HiGHS minimization marginals 因此满足 `lambda_h <= 0`。任意未来 column：

`rc = c - pi*a - lambda*(s*b)`。

Pricing MILP 把 `-lambda_h s_h` 加到每个匹配的 start arc、inter-position arc 与 return arc variable。每次 traversal 都接收 coefficient，因此 repeated traversals 是 exact。每次 pricing call 都携带全部 ancestor rows。

`solve_fully_priced_node()` 反复求 true node RMP 与全部 3 bases × 3 types exact pricing MILPs。只有九项全部 `Optimal` 且没有 reduced cost 低于 `PRICING_TOL=1e-7`，节点才是 `FULLY_PRICED`。

## Phase-I / Farkas gate

Restricted RMP infeasibility 不是证书。引擎会切换到 elastic Phase-I：

- demand rows：`A x + p - n = d`；
- branch rows：`alpha x - v <= beta`；
- objective：`min sum(p+n+v)`；
- real route columns cost = 0。

Phase-I exact pricing 使用相同 demand/branch dual 公式，但 `route_cost_multiplier=0`。发现 negative real column 就加入并继续。只有 no-negative pricing 加正 artificial optimum 才证明完整 node infeasible；artificial optimum 为零则返回 true Phase-II LP。

## Bound semantics and fathoming

Unpriced child 只存 inherited parent bound，provenance 为 `INHERITED`。Fully-priced feasible node 才存自身 LP bound。Global LB 是所有 open nodes 当前 rigorous bounds 的最小值。

Conservative bound rule 为 `ceil(node_lb - CERT_TOL) >= GLOBAL_UB`，其中 `CERT_TOL=1e-6`。其他 tolerance 分离：`LP_FEAS_TOL=1e-8`、`INTEGER_TOL=1e-7`、`PRICING_TOL=1e-7`。

Node integer RMP 仅是 primal heuristic。它使用 `GLOBAL_UB-1` cutoff；非法 frozen start 会被检测且不提交。Candidate 只有物化并通过 independent Validator 后才更新 UB，永远不产生 LB 或 fathom。

## Registry, queue and resume

所有 generated columns 进入 global semantic-identity registry。复用于其他 node 时，从 ordered stops 重新计算全部 active branch coefficients。Queue 为 deterministic best-bound-first，tie-break 使用 depth/node-id。

Checkpoint 保存 validated UB、global LB、global exact columns、processed/open nodes、完整 branch histories、bounds/provenance、pricing/CG counts、runtime、progress、source SHA 与 dirty state。Resume 重建 solver models，不序列化 solver pointers。


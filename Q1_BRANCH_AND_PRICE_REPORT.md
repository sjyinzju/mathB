# Q1 Branch-and-Price Report

最终状态：**BRANCH_AND_PRICE_INCOMPLETE**。没有生成全局最优性证书，也没有把 14,730 称为 global optimum。

## Current Foundation

起点为 `b0dffe1fdd39184e3d627eaeaef7645d67b36a14`。Full-space root LP lower bound 为 `14090.32748538012`，validated UB 为 `14730`，rigorous gap 为 `4.3426512%`。初始 B&P checkpoint 含 fully-priced root 1 个、open unpriced children 2 个。

## 1,041-column Integer Master

最终 fully-priced root pool 含 1,041 列。14,730 frozen start 映射到 84 个非零变量，coverage residual 0、missing patterns 0，HiGHS 接受完整 start。

Normal minimization 作为 300 秒 primal check，处理 82,300 nodes，best feasible 为 14,730，independent Validator 为 VALID、0 issues。随后 hard row `aircraft time <= 14729` 的 strict model 无固定时限完整搜索：HiGHS 1.15.1 返回 `Infeasible`，449,347 nodes、27,847,090 LP iterations、2,771.92 秒。因此 14,730 是当前 1,041-column fully-priced root pool 的严格 integer optimum；该结论不外推到完整整数列空间。

## Branch-aware Exact Pricing

Branch row 统一为 canonical `s b x <= s k`：`s=+1` 对 `<=`，`s=-1` 对 `>=`。在 minimization LP 中 canonical inequality dual `lambda <= 0`，节点 reduced cost 为：

`rc(r) = c_r - sum_g pi_g a_gr - sum_h lambda_h s_h b_hr`。

全部 ancestor rows 都进入 RMP 和 pricing。`b_hr` 是 ordered physical route 中 directed arc 的真实 traversal count；start、inter-position、return 和 repeated traversals 使用同一定义。未来生成列由完整 stops 自动计算全部 branch coefficients。

新增 13 个 branch-specific tests：左/右符号、2/3 层 ancestors、5 个 randomized exhaustive cases、repeated traversal count=2、adjacent self-transition、RMP dual sign、Phase-I objective。原 pricing tests 加新增 tests 共 22/22 通过；全仓 81/81 通过。Branch-aware oracle 状态为 **CERTIFIED EXACT for the existing complete Q1 column definition**。

若 restricted node RMP infeasible，引擎不会直接 fathom，而会建立 elastic Phase-I：demand equality 使用正负 artificial variables，branch row 使用 violation artificial，real columns 的 cost multiplier 为 0。只有 Phase-I 的九个 exact pricing 全部通过 no-negative gate 且 artificial objective 严格为正，才允许 `FATHOM_BY_INFEASIBILITY`。

## Root Children Gate

| Node | Branch history | Initial RMP | Fully-priced LB | CG rounds | Pricing calls | New columns | Fractional vars | Result |
|---|---|---:|---:|---:|---:|---:|---:|---|
| N1-L | A02→F024 <= 3 | 14091.290351 | 14090.327485 | 2 | 18 | 1 | 66 | fractional, not fathomed |
| N1-R | A02→F024 >= 4 | 14111.477671 | 14092.684211 | 3 | 27 | 4 | 68 | fractional, not fathomed |

两个节点最后一轮 9/9 pricing 均为 `Optimal`，minimum reduced cost = 0，新增列 = 0；因此两个 `OPEN_UNPRICED` child 已真正转为 `FULLY_PRICED`。

## Recursive Search

Node selection 为 best-bound-first，然后浅 depth、node id。默认 branching 继续使用最接近半整数的 aggregate directed-arc usage。

首个递归节点 `N2-LL` 的 history 为 `A02→F024 <=3, <=2`。该节点经过 2 轮、18 次 exact pricing、生成 1 列后 fully priced，LB 为 `14090.327485380118`，含 66 个 fractional variables，不能 fathom；30 秒 node integer RMP 没有找到 strict improvement。它在同一 arc 的 `1.5204678363` usage 上生成 `N3-LLL <=1` 与 `N3-LLR >=2`。

当前统计：fully-priced processed nodes = 4（root、N1-L、N1-R、N2-LL）；open nodes = 5；bound/infeasibility/integrality fathomed 均为 0；max processed depth = 2，max open depth = 3；global registry = 1,047 columns；root 后 branch-specific generated columns = 6。完整 root pricing 144 次，加 branch-aware pricing 63 次，总 exact pricing calls = 207；总 exact generated columns = 44。

当前 open queue：`N2-LR`、`N2-RL`、`N2-RR`、`N3-LLL`、`N3-LLR`。未 fully-priced nodes 仅使用 inherited rigorous bound，未冒充 child LP bound。

## Primal Incumbents

1,046-column node integer heuristics 使用 `<=14729` cutoff。N1-L 60 秒返回 UNKNOWN，N1-R 36.43 秒证明当前 node pool infeasible；N2-LL 30 秒返回 UNKNOWN。三者都没有候选，因此没有调用 ALNS education，也没有 UB 更新。

Validated incumbent 仍为 frozen 14,730 / passenger 121,363 / 89 flights / 118,624.4 kg / utilization 0.4942969225 / served 1600/1600 / VALID 0 issues。

## Runtime and Bottleneck

三个 branch-aware fully-priced nodes的 RMP 时间合计 0.12622 秒，exact pricing 时间合计 3,423.323801 秒；在二者合计中 pricing 占 **99.9963%**。N1-L、N1-R、N2-LL elapsed 分别为 682.51、1,524.06、1,216.97 秒。继续逐节点使用当前 position-indexed MILP oracle 没有改善 global LB，并会以约 10–25 分钟/节点扩张树。

Limited strong branching、pseudo-cost 和 dual stabilization 没有启用。原因不是将 probe 当证明，而是每个 exact node pricing 已是绝对瓶颈；此时增加多 child probes 会成倍放大成本。当前最值得做的唯一 proof optimization 是实现 exact DP/label-setting pricing oracle，并以现有 MILP oracle进行 exhaustive/randomized/full-instance cross-check；性能下降到可接受水平后，再恢复 checkpoint 并加入 limited strong branching。

## Final Proof State

Global LB = `14090.327485380118`，validated Global UB = `14730`，rigorous gap = `4.3426512%`。Objective 为整数分钟，bound fathom 使用 `ceil(LB - 1e-6) >= UB`；当前不满足。剩余 open nodes = 5。

`GLOBAL_OPTIMALITY_STATUS = BRANCH_AND_PRICE_INCOMPLETE`。

机器证据：

- `outputs/q1/exact/integer-master/20260816-final-1041-master/`
- `outputs/q1/exact/branch-and-price/20260816-root-children/`
- `outputs/q1/exact/branch-and-price/20260816-node-integer-heuristics/`
- `outputs/q1/exact/branch-and-price/20260816-recursive-best-bound/checkpoint.json`

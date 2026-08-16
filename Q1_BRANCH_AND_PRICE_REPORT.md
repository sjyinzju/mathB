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

### Fast Oracle Resumption（2026-08-16 晚）

Fast exact pricing oracle 认证通过（见 `Q1_FAST_PRICING_REPORT.md`，Gate 8.2 36/36，commit `0bf403a`）后，checkpoint `20260816-recursive-best-bound` 恢复。截至 20:27，fully-priced processed nodes 共 9 个（root、N1-L、N1-R、N2-LL + 恢复后 8 个：N3-LLL、N3-LLR、N4-LLRL、N4-LLRR、N2-LR、N3-LRL、N4-LRLL、N4-LRLR）；open nodes = 13；bound/infeasibility/integrality fathomed 均为 0；max processed depth = 4；global registry = 1,052 columns；root 后 branch-specific generated columns = 11。

原始 5 个 open nodes 中 3 个已 fully priced（N3-LLL、N3-LLR、N2-LR）；N2-RL、N2-RR（inherited LB 14092.684）被 best-bound 排序持续压后，因为每个已处理节点都派生一个 LB ≈ 14090.327 的 child，低界侧队列不减反增。

**Arc branching bound gain 实测（恢复后 8 节点）**：同一 arc 左子 +1.041、右子 0.000 的退化模式反复出现（N3-LLL/N3-LLR、N4-LLRL/N4-LLRR、N4-LRLL/N4-LRLR 为 +1.041/+0.000 或 +1.041/+2.357），平均 gain ≈ 0.82 分钟/节点，而 bound fathom 需要约 640 分钟的界提升。树按每处理 1 节点 +1 open 的速率自然爆炸。这正是 handoff §15 预判的「Arc Branching 加强后仍树爆炸」场景：pricing 已快（401–1,217 秒/节点），processed nodes 已成规模，但 Global LB 提升极慢 → 下一阶段是 P2 Valid Inequality / Cut Audit。

当前 open queue（13）：`N2-RL`、`N2-RR`、`N4-LLLL`、`N4-LLLR`、`N5-LLRLL`、`N5-LLRLR`、`N5-LLRRL`、`N5-LLRRR`、`N3-LRR` 及后续派生节点。未 fully-priced nodes 仅使用 inherited rigorous bound，未冒充 child LP bound。搜索进程在后台持续运行并逐节点 checkpoint。

## Primal Incumbents

1,046-column node integer heuristics 使用 `<=14729` cutoff。N1-L 60 秒返回 UNKNOWN，N1-R 36.43 秒证明当前 node pool infeasible；N2-LL 30 秒返回 UNKNOWN。三者都没有候选，因此没有调用 ALNS education，也没有 UB 更新。

Validated incumbent 仍为 frozen 14,730 / passenger 121,363 / 89 flights / 118,624.4 kg / utilization 0.4942969225 / served 1600/1600 / VALID 0 issues。

## Runtime and Bottleneck

MILP 时代（N1-L、N1-R、N2-LL）：RMP 合计 0.12622 秒，exact pricing 合计 3,423.32 秒，pricing 占 **99.9963%**；节点 elapsed 682.51、1,524.06、1,216.97 秒。

Fast oracle 恢复后：8 个新节点 pricing 合计 6,145 秒（401–1,359 秒/节点，均值 ~768 秒），Gate 认证口径下 fast/MILP 总墙钟 1,798.6 s / 5,425.2 s = **3.0x**。pricing 仍是主导成本，但已不是速率限制；新的速率限制是 **branching 质量**（界增益 ~0.82 分钟/节点 vs 需要 ~640 分钟）。

Limited strong branching、pseudo-cost 和 dual stabilization 没有启用。P0 阶段的目标（exact DP pricing oracle + 完整 cross-check + checkpoint 恢复）已全部完成；下一唯一值得做的是 P2 cut audit：分析 fractional root/node 解为何 LP 能到 14,090 而 integer 必须接近 14,730，寻找对本 aggregated allocation formulation 有效且 pricing coefficient 可精确处理的 valid inequalities（见 `Q1_FAST_PRICING_REPORT.md` §7 与 handoff §P2）。

## Final Proof State

Global LB = `14090.32748538012`，validated Global UB = `14730`，rigorous gap = `4.3426512%`。Objective 为整数分钟，bound fathom 使用 `ceil(LB - 1e-6) >= UB`；当前不满足。剩余 open nodes = 13（后台搜索持续中，逐节点 checkpoint）。

`GLOBAL_OPTIMALITY_STATUS = BRANCH_AND_PRICE_INCOMPLETE`。

`FAST_PRICING_STATUS = CERTIFIED_EXACT`（2026-08-16，Gate 8.2 36/36 + 16/16 单测 + 400-trial Pareto 属性测试；详见 `Q1_FAST_PRICING_REPORT.md`）。

机器证据：

- `outputs/q1/exact/integer-master/20260816-final-1041-master/`
- `outputs/q1/exact/branch-and-price/20260816-root-children/`
- `outputs/q1/exact/branch-and-price/20260816-node-integer-heuristics/`
- `outputs/q1/exact/branch-and-price/20260816-recursive-best-bound/checkpoint.json`

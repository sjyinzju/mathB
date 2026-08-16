# Q1 Exact Certification Handoff

分支 `codex/q1-exact-certification` 从 frozen `be109e6` 创建。Frozen tag `q1-final-or-14730` 未修改。

已完成：direct HiGHS MIP-start reproduction；strict 14,729 restricted master 完整证明 infeasible；完整 Q1 primary column specification；3×3 exact pricing；tiny/random exhaustive validation；16 轮 full-space exact CG；root B&P branching/checkpoint。

关键结果：restricted 14,730 optimum proven；full-space LP LB = 14,090.32748538012；ceil = 14,091；validated UB = 14,730；rigorous gap = 4.3426512%；B&P root processed 1、open 2。Global status 是 `BRANCH_AND_PRICE_INCOMPLETE`，绝非 global optimum。

下一步只应实现 child arc row + dual-aware exact pricing，然后 best-bound 递归。每个 node 必须 fully price 后才能使用其 node LB/fathom；integer candidate 必须 materialize + independent Validator。不要生成 `Q1_GLOBAL_OPTIMALITY_CERTIFICATE.md`，除非所有 open nodes rigorously fathomed 且 ceil(global LB) ≥ validated UB。

实现入口：`src/solver/q1_exact.py`、`src/solver/q1_pricing.py`、scripts 14–17。机器 artifacts 位于 `outputs/q1/exact/`。

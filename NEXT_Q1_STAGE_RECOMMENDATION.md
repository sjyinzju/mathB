# 下一阶段 Q1 建议

## 决策：CASE A（极窄），随后冻结

Route Pool / ALNS 本阶段确实从 14,770 降到 14,730，说明 classical OR 尚不能说“完全没有空间”；但 pool 已从 95 增到 98 sources 而物理 routes 停在 391，多个 elite basins 和 targeted neighborhoods 也已平台。因此不是继续广泛搜索，而是只允许 **一次聚焦 classical master intensification**。

## 唯一最值得继续的方法

对 exact allocated-route-pattern master：

1. 消除同 physical route / equivalent allocated patterns 的整数对称性；
2. 使用支持 incumbent / MIP start 的求解后端，把当前 14,730 solution 作为 warm start；
3. 施加 strict primary upper bound 14,729；
4. 保持 391-route frozen pool，或只接收由明确 high-dual结构产生且经 shared evaluator 验证的新列；
5. 报告 found incumbent、restricted infeasibility proof 或 unresolved，严格区分三者。

原因：SciPy `milp` 无 warm start，Round 5 的 600 秒 no-incumbent 无法判断是不可行还是求解效率问题；final restricted MIP dual 14,705、LP 14,208.64 又显示整数性/组合仍有空间。

## 不进入 ML

ML readiness 为 **NOT_READY**：19 个 evaluated candidate events、1 个 positive、1 个 lineage。现在训练 LR 或 LightGBM 会得到不可可靠分组验证的小样本结论。因此下一阶段 **不进入 LR → LightGBM**。若未来自然积累多个 runs/lineages、足够 positives，再按 run/lineage group split 重新审查 readiness；禁止 candidate random split。

## 不进入 pricing / Branch-and-Price

本阶段 master 用既有列持续产生 14,743 → 14,730 的改善，LP fractionality 也主要表现为 set-partitioning/allocation combination。缺少 heuristic pricing 持续发现 useful columns 的证据，因此不实现完整 CG/B&P。只有聚焦 master 之后出现明确 missing-column bottleneck，且 exact pricing subproblem 能清晰定义时，才重新评估。

## Freeze gate

完成上述一次聚焦 master 后，不论结果是 `<14,730`、restricted infeasible 或 unresolved，均应冻结 Q1 的广泛算法研究，并保留当前 14,730 VALID artifacts 作为安全 incumbent。不要重新开展 PAM/K-means、hard clustering、fuel/composite relatedness、SA cooling/weight 大网格或大量随机 seeds。

# STAGE: 共享性能基座统一 + 算法分支集成审计 — 交接文档

日期：2026-08-15 ｜ 上一份交接：STAGE_SHARED_SOLVER_PERFORMANCE_HANDOFF.md（d28e982）

## 本阶段完成了什么

**阶段 A（已落地）**：main 已吸收共享性能基础设施（LegPhysics / SolverCache / technical_stops 提速），main = `d28e982`。
15,371 回归 gate 全过：pytest 41 项、字节级 CSV 复现一致、--start-best 收敛 accepted_moves=0、Validator VALID、cache 统计正常（augmentation hits 1350 / misses 417）。

**阶段 B（审计完成，等待批准）**：两个算法分支的非破坏性集成演练均已跑通，正式分支未做任何改动。
- clustering：演练分支 `audit/q1-clustering-main-integration` @ `7b900ae`（worktree `%TEMP%\clustering-integration`）
- ALNS（基线锁定 `7d04432`）：演练分支 `audit/q1-alns-main-integration` @ `8263d51`（worktree `%TEMP%\alns-audit-7d04432`）
- 批准清单与执行方式见 INTEGRATION_APPROVAL_PLAN.md

## 关键结论

1. **clustering 集成无语义风险**：仅 improve.py/__init__.py 两处真冲突；融合后 k=3/b25 回归与 Phase-1 存档字节级一致（结果、candidate_events、routes 全同），decision 同为 abandon_mainline。提速使 timing 指标变化，不影响候选排序语义。
2. **ALNS 集成无语义风险**：固定 seed=0/iterations=12/同一初解的 before/after 对照中，operator 序列、accept 判定、best 轨迹完全一致；assignments/operator_stats/metrics.json 字节一致；routes 行集合一致（仅导出顺序差异）；convergence 仅 elapsed_seconds 列不同（提速 2.5×）。
3. **variant_cache vs SolverCache**：augmentation 层 key 完全重叠，已重接共享 SolverCache；RouteVariant/variant_cache 保留（含 arrival_minutes/capacity，是 MILP repair 专属上下文，不可并入通用缓存）。
4. **ALNS 正式分支有 Q2 增量**：origin/platinumist_update = `c37ea9b`，7d04432 之后的 Q2 commits 本轮未审计；批准 ALNS 集成前需先决策落地方式（见批准计划风险项）。

## 接手者须知

- 两个 worktree 在 `%TEMP%` 下，重启可能丢失；若批准后要执行，先确认 worktree 存在，否则从分支重建（分支已含全部演练成果）。
- worktree 内 best CSV 需 LF 换行（autocrlf 检出为 CRLF 会导致 pytest 失败）——演练时已转好，重建时需重做。
- ALNS golden 配置（seed 0/1，iter 60，两阶段）目前从 B1/15,743 起步；下阶段需改从 15,371（best）起步 multi-seed 重跑。
- clustering Phase-1 结论本身未变：两个入选 ranker 均为 abandon_mainline，集成不改变该结论。

## 下一步（按用户批准触发）

| 触发 | 动作 |
|---|---|
| "批准 clustering" | 在 codex/q1-clustering 上按 7b900ae 方式 merge main，重跑 50 项 pytest + b25 对照 |
| "批准 ALNS@7d04432" | 按 8263d51 方式在 7d04432 基线落地（默认方式 A：新建分支，不动 platinumist_update HEAD） |
| 两者皆批 | 依次执行上述两项 |
| 下阶段 | ALNS 从 15,371 起步 multi-seed；Q2 commits（c37ea9b）单独审计 |
